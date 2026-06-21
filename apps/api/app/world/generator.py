"""Agent-generated world — thin harness over a fat skill (skill.md).

One gateway call turns a seed into a richer ``WorldSpecLite``. The harness here
stays deliberately thin: load the skill prompt, ask the model for JSON, repair +
parse it (reusing the debate judge's json-repair pattern), validate against the
frozen ``WorldSpecLite`` contract, and cache by seed. On ANY failure it returns
``None`` so the caller falls back to the Wave-2 procedural world. It NEVER raises
to the caller — the generator can never break the game.

Public surface:
    await generate_world(seed, width, height, *, model="default") -> WorldSpecLite | None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.gateway.gateway import gateway
from app.schemas import WorldSpecLite

try:  # json_repair is in the venv; degrade gracefully if absent (same as judge.py)
    from json_repair import repair_json
except Exception:  # noqa: BLE001
    repair_json = None  # type: ignore[assignment]


_SKILL_PATH = Path(__file__).with_name("skill.md")

# In-process cache: same seed -> same world, generated at most once. Keyed by
# (seed, width, height) so a different grid size doesn't return a stale world,
# while a repeat of the exact request is served without re-invoking the gateway.
_CACHE: dict[tuple[int, int, int], WorldSpecLite] = {}


def _load_skill() -> str:
    """Load the fat-skill generation prompt. Cached in module memory after first read."""
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — missing skill file must not crash the run
        return ""


# Read once at import; harmless if the file is absent (-> empty skill, harness
# still emits a usable instruction and otherwise just falls back).
_SKILL = _load_skill()


def _parse_json(raw: str) -> Any:
    """Best-effort JSON parse, reusing the judge's repair ladder.

    1. ``json.loads`` for already-valid output.
    2. ``json_repair.repair_json`` to salvage malformed/truncated small-model
       output (the exact pattern from app.debate.judge._parse_json).
    Returns ``None`` if nothing parseable can be recovered.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    if repair_json is not None:
        try:
            repaired = repair_json(raw)
            return json.loads(repaired) if isinstance(repaired, str) else repaired
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_messages(seed: int, width: int, height: int) -> list[dict[str, str]]:
    """Compose the (system=skill, user=task) messages for the gateway call."""
    system = _SKILL or (
        "You are a roguelike level designer. Emit ONLY a JSON object describing a "
        "tile world with regions, pois (camp/town/den/landmark/start/goal), a start "
        "and a goal."
    )
    user = (
        f"Design a world for seed={seed} on a {width}x{height} tile grid "
        f"(x: 0..{width - 1}, y: 0..{height - 1}).\n"
        f"Echo seed={seed}, width={width}, height={height} in the JSON.\n"
        "Output ONLY the JSON object described in the schema — no prose, no fences."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _coerce_spec(
    data: Any, seed: int, width: int, height: int
) -> WorldSpecLite | None:
    """Validate parsed data into a WorldSpecLite, pinning seed/width/height.

    The grid dimensions and seed are authoritative from the request, not the
    model — we overwrite them so the world is always consistent with what the
    caller asked for (and stays seed-deterministic per the contract). Returns
    ``None`` on any validation failure.
    """
    if not isinstance(data, dict):
        return None
    data = dict(data)  # shallow copy; don't mutate the caller's object
    data["seed"] = seed
    data["width"] = width
    data["height"] = height
    try:
        return WorldSpecLite.model_validate(data)
    except Exception:  # noqa: BLE001 — pydantic ValidationError or anything else
        return None


async def generate_world(
    seed: int,
    width: int,
    height: int,
    *,
    model: str = "default",
) -> WorldSpecLite | None:
    """Generate a world for ``seed`` via one gateway LLM call.

    Returns a validated ``WorldSpecLite`` on success, or ``None`` on ANY failure
    (gateway error, empty/bad JSON, validation failure, timeout). Never raises —
    the caller is expected to fall back to the procedural world on ``None``.

    Successful results are cached per (seed, width, height): a repeat request
    returns the cached world WITHOUT re-invoking the gateway, so the same seed
    always yields the same world.
    """
    key = (seed, width, height)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        raw = await gateway.complete(
            _build_messages(seed, width, height),
            model=model,
            temperature=0.4,
            max_tokens=1024,
            json_mode=True,
        )
    except Exception:  # noqa: BLE001 — network/model/timeout -> fall back
        return None

    data = _parse_json(raw)
    spec = _coerce_spec(data, seed, width, height)
    if spec is None:
        return None

    _CACHE[key] = spec
    return spec


def _clear_cache() -> None:
    """Test/maintenance hook: drop all cached worlds."""
    _CACHE.clear()
