"""Background Wikipedia hydration for gacha-pulled personas (Wave A).

Pull flow:
    1. `app/routers/gacha.py` inserts the Monster row with `wiki_hydrated=False`
       and a tagline-only persona stub.
    2. It schedules `asyncio.create_task(hydrate_monster(...))` so the request
       returns instantly.
    3. This module fetches Wikipedia's `/page/summary` endpoint (cheap, clean
       300-char extract), distills it via a single gemma3:1b call, writes the
       result to `apps/api/.cache/personas/{key}.json`, then patches the
       Monster row's persona JSONB and flips `wiki_hydrated=True`.

Robustness contract (matches `app/debate/coach.py`):
    - This function MUST NEVER raise into the caller. Every failure path
      (no httpx, network down, model down, cache write fails, monster gone)
      degrades to a fallback patch with the seed tagline as `voice` and an
      empty views/quotes/domain_keywords list.
    - `wiki_hydrated` is flipped True even on failure so the frontend poll
      exits its waiting state and the player can battle.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("uvicorn.error")

# Disk cache. Located inside the apps/api tree so it's container-local but
# survives across process restarts in dev. Pre-warmed by an optional script.
_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "personas"

# Wikipedia REST `/page/summary` returns a clean extract that's already
# ~300-500 characters — much friendlier to a small local model than scraping.
_WIKI_SUMMARY_TIMEOUT_S = 5.0
_LLM_DISTILL_TIMEOUT_S = 20.0
_LLM_DISTILL_MAX_TOKENS = 200
_LLM_DISTILL_TEMPERATURE = 0.3


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def hydrate_monster(
    monster_id: str,
    wiki_url: str,
    fallback_tagline: str,
) -> None:
    """Hydrate a gacha-pulled monster's persona from Wikipedia in the background.

    Best-effort end to end. Errors are caught and logged; on any failure the
    monster is still patched with a fallback persona blob and ``wiki_hydrated``
    flips to True so the frontend poll exits.
    """
    persona_key = _persona_key_from_url(wiki_url) or monster_id
    distilled: dict[str, Any] = {}

    # 1. Try disk cache.
    cached = _cache_read(persona_key)
    if cached is not None:
        distilled = cached
    else:
        # 2. Fetch + distill.
        try:
            wiki_text = await _fetch_wiki_summary(wiki_url)
            distilled = await _llm_distill(wiki_text, fallback_tagline)
            # 3. Cache for next time (the seed catalog has ~30 entries, so the
            #    warm-up case is the common one after a dev session). Only
            #    cache real distill output — never cache an empty fallback,
            #    or we'd lock the cache into the failure branch.
            if distilled:
                _cache_write(persona_key, distilled)
        except Exception as e:  # noqa: BLE001
            log.info("hydrate_monster[%s]: fetch/distill failed (%s)", persona_key, e)
            distilled = {}

    # Ensure required keys are present even on partial successes.
    patched = _ensure_shape(distilled, fallback_tagline)

    # 4. Patch DB (also best-effort).
    try:
        await _patch_monster(monster_id, patched)
    except Exception as e:  # noqa: BLE001
        log.warning("hydrate_monster[%s]: patch failed (%s)", monster_id, e)


# ---------------------------------------------------------------------------
# Wikipedia fetch
# ---------------------------------------------------------------------------


def _summary_url(wiki_url: str) -> Optional[str]:
    """Convert a canonical Wikipedia page URL into the REST `/page/summary` URL.

    Returns None for inputs that don't match the expected ``/wiki/<title>`` shape.
    """
    if not wiki_url:
        return None
    m = re.match(r"^(https?://[^/]+)/wiki/(.+)$", wiki_url.strip())
    if not m:
        return None
    host, title = m.group(1), m.group(2)
    # `/api/rest_v1/page/summary/<title>` is the modern REST summary endpoint.
    return f"{host}/api/rest_v1/page/summary/{title}"


async def _fetch_wiki_summary(wiki_url: str) -> str:
    """Fetch the Wikipedia page summary extract. Returns "" on any failure."""
    target = _summary_url(wiki_url)
    if not target:
        return ""
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""

    async with httpx.AsyncClient(timeout=_WIKI_SUMMARY_TIMEOUT_S) as client:
        try:
            resp = await client.get(
                target,
                headers={
                    # Wikipedia asks for a descriptive UA on REST clients.
                    "User-Agent": "debate-rpg-hydrate/1.0 (hackathon demo)",
                    "Accept": "application/json",
                },
            )
        except Exception as e:  # noqa: BLE001
            log.info("hydrate: wiki fetch raised (%s)", e)
            return ""
    if resp.status_code != 200:
        return ""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return ""
    # The summary endpoint returns at least `extract`; sometimes `description`
    # is a one-line tagline. Concatenate for the LLM with a clear separator.
    extract = (data.get("extract") or "").strip()
    description = (data.get("description") or "").strip()
    if extract and description and description.lower() not in extract.lower():
        return f"{description}\n\n{extract}"
    return extract or description


# ---------------------------------------------------------------------------
# LLM distill
# ---------------------------------------------------------------------------


_DISTILL_SYSTEM = (
    "You are a precise summarizer building a debate persona profile from a "
    "Wikipedia summary. Respond with STRICT JSON only — no prose, no markdown "
    "fence — matching this shape exactly:\n"
    '{"voice": "<one short sentence on how they speak>", '
    '"views": ["<3-5 short stances>"], '
    '"quotes": ["<2-3 short quotes>"], '
    '"domain_keywords": ["<3-6 single-word topic tags>"]}'
)


def _distill_user_prompt(wiki_text: str, fallback_tagline: str) -> str:
    body = wiki_text.strip() or fallback_tagline.strip()
    return (
        "Wikipedia summary:\n"
        f"{body[:3000]}\n\n"
        "Return the persona profile JSON now."
    )


async def _llm_distill(wiki_text: str, fallback_tagline: str) -> dict[str, Any]:
    """Single gemma3:1b distill call returning the persona blob. Defensive."""
    if not wiki_text:
        return {}
    try:
        from app.gateway.gateway import gateway  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}

    messages = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": _distill_user_prompt(wiki_text, fallback_tagline)},
    ]
    try:
        raw = await gateway.complete(
            messages,
            model="gemma3:1b",
            temperature=_LLM_DISTILL_TEMPERATURE,
            max_tokens=_LLM_DISTILL_MAX_TOKENS,
            timeout=_LLM_DISTILL_TIMEOUT_S,
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001
        log.info("hydrate: distill call failed (%s)", e)
        return {}

    return _safe_parse_json(raw)


def _safe_parse_json(raw: str) -> dict[str, Any]:
    """Parse the model's output as JSON; degrade to {} on any structural issue."""
    text = (raw or "").strip()
    if not text:
        return {}
    # Strip code fences a small model might emit despite json_mode.
    if text.startswith("```"):
        text = text.strip("`")
        # drop any leading "json\n" tag
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        # Try to recover the first {...} block.
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return data if isinstance(data, dict) else {}


def _ensure_shape(distilled: dict[str, Any], fallback_tagline: str) -> dict[str, Any]:
    """Coerce the distill output into the canonical persona shape, filling defaults."""
    voice = (distilled.get("voice") or "").strip() or (fallback_tagline or "").strip()
    views = [str(v) for v in (distilled.get("views") or []) if v][:6]
    quotes = [str(q) for q in (distilled.get("quotes") or []) if q][:5]
    keywords = [str(k) for k in (distilled.get("domain_keywords") or []) if k][:8]
    return {
        "voice": voice,
        "views": views,
        "quotes": quotes,
        "domain_keywords": keywords,
    }


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _persona_key_from_url(wiki_url: str) -> Optional[str]:
    """Extract a stable cache key (~persona slug) from the wiki URL."""
    if not wiki_url:
        return None
    m = re.match(r"^https?://[^/]+/wiki/(.+)$", wiki_url.strip())
    if not m:
        return None
    title = m.group(1).split("#", 1)[0].split("?", 1)[0]
    # Lowercased + only-safe chars for filenames.
    return re.sub(r"[^a-z0-9_]+", "_", title.lower()).strip("_") or None


def _cache_path(persona_key: str) -> Path:
    return _CACHE_DIR / f"{persona_key}.json"


def _cache_read(persona_key: str) -> Optional[dict[str, Any]]:
    path = _cache_path(persona_key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _cache_write(persona_key: str, data: dict[str, Any]) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path(persona_key)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log.info("hydrate: cache write failed for %s (%s)", persona_key, e)


# ---------------------------------------------------------------------------
# DB patch
# ---------------------------------------------------------------------------


async def _patch_monster(monster_id: str, persona_blob: dict[str, Any]) -> None:
    """Open a fresh session, merge the distilled blob into Monster.persona,
    flip ``wiki_hydrated`` True, and bump ``genome_version``.
    """
    try:
        from app.db.models import Monster  # noqa: PLC0415
        from app.db.session import SessionLocal  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        log.warning("hydrate: cannot import session (%s)", e)
        return

    async with SessionLocal() as session:
        monster = await session.get(Monster, monster_id)
        if monster is None:
            log.info("hydrate: monster %s gone before patch", monster_id)
            return
        merged = dict(monster.persona or {})
        merged.update(persona_blob)
        monster.persona = merged
        monster.wiki_hydrated = True
        try:
            monster.genome_version = (monster.genome_version or 1) + 1
        except Exception:  # noqa: BLE001
            pass
        session.add(monster)
        await session.commit()
