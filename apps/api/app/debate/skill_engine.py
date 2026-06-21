"""Skill engine — loads rhetorical "skill.md" moves and exposes them as
turn-by-turn prompt guidance for the debate orchestrator.

VISION: each command (e.g. "Credential Drop") is a real ``skill.md`` the agent
executes as a turn to turn the debate in its favour. This module loads those
markdown move files from ``app/skills/`` (one per move), caches them, and serves
the move's instruction text on demand.

Public surface (kept STABLE — the orchestrator imports this directly):

    skill_instructions(skill_name: str | None, attacker_type: str | None = None) -> str
    skill_cost(skill_name: str | None) -> int     # gacha wave (MP economy)
    skill_costs() -> dict[str, int]               # slug -> mp_cost (for bulk-update)

Behaviour:
  * Slug-match ``skill_name`` against ``app/skills/<slug>.md`` and return the
    move's instruction body (front-matter stripped).
  * If the named skill is unknown but ``attacker_type`` is given, fall back to a
    generic by-type ("domain") instruction built from the type's signature
    moves.
  * Return ``""`` (never raise) when nothing matches — the orchestrator treats an
    empty string as "no extra guidance".
  * ``skill_cost`` reads ``mp_cost: <int>`` from the same front-matter; default 0.

The loader reads each ``.md`` once and caches it; everything is defensive so a
missing directory, unreadable file, or odd name degrades to "" rather than an
exception.
"""
from __future__ import annotations

import re
from pathlib import Path

try:  # type/domain registry lives with the archetypes (single source of truth)
    from app.party.archetypes import TYPE_DOMAINS, domain_for_type
except Exception:  # noqa: BLE001 — never let an import problem break the engine
    TYPE_DOMAINS = {}  # type: ignore[assignment]

    def domain_for_type(_dt):  # type: ignore[misc]
        return {"description": "", "signature_skills": []}


#: Directory holding the per-move ``.md`` files (sibling ``app/skills``).
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

#: slug -> instruction text (front-matter stripped). Populated lazily, once.
_CACHE: dict[str, str] | None = None

#: slug -> parsed MP cost (front-matter ``mp_cost``; 0 when absent). Gacha wave.
_COST_CACHE: dict[str, int] | None = None


def slugify(name: str | None) -> str:
    """Normalise a skill display name to a filename slug.

    "Credential Drop" -> "credential_drop". Non-alphanumerics collapse to single
    underscores; result is lower-cased and trimmed. Empty/None -> "".
    """
    if not name:
        return ""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return s.strip("_")


def _split_front_matter(text: str) -> tuple[str, str]:
    """Return ``(front_matter, body)`` for a ``---``-fenced markdown file.

    ``front_matter`` is the raw YAML-ish block between the two fences (empty
    string when no fence is present). ``body`` is everything after the closing
    fence (or the whole text when there's no front matter), trimmed.
    """
    if text.startswith("---"):
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        if len(parts) >= 3:
            return parts[1].strip(), parts[2].strip()
    return "", text.strip()


def _strip_front_matter(text: str) -> str:
    """Remove a leading ``---`` YAML front-matter block, if present."""
    return _split_front_matter(text)[1]


# Match ``mp_cost: <int>`` on its own front-matter line. Tolerates whitespace
# and integer-only values (the rest of the front matter is ignored).
_MP_COST_RE = re.compile(r"^\s*mp_cost\s*:\s*(-?\d+)\s*$", re.MULTILINE)


def _parse_mp_cost(front_matter: str) -> int:
    """Pull ``mp_cost`` out of the raw front matter; default 0, never raise."""
    if not front_matter:
        return 0
    m = _MP_COST_RE.search(front_matter)
    if not m:
        return 0
    try:
        return max(0, int(m.group(1)))
    except (TypeError, ValueError):
        return 0


def _load_caches() -> tuple[dict[str, str], dict[str, int]]:
    """Read every ``app/skills/*.md`` once; cache slug -> body AND slug -> cost."""
    global _CACHE, _COST_CACHE
    if _CACHE is not None and _COST_CACHE is not None:
        return _CACHE, _COST_CACHE
    body_cache: dict[str, str] = {}
    cost_cache: dict[str, int] = {}
    try:
        if _SKILLS_DIR.is_dir():
            for path in sorted(_SKILLS_DIR.glob("*.md")):
                try:
                    raw = path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001 — skip unreadable file, keep going
                    continue
                front, body = _split_front_matter(raw)
                slug = path.stem.lower()
                if body:
                    body_cache[slug] = body
                cost_cache[slug] = _parse_mp_cost(front)
    except Exception:  # noqa: BLE001 — directory iteration failure -> empty caches
        body_cache = {}
        cost_cache = {}
    _CACHE = body_cache
    _COST_CACHE = cost_cache
    return _CACHE, _COST_CACHE


def _load_cache() -> dict[str, str]:
    """Back-compat alias for the body-only cache used by ``skill_instructions``."""
    return _load_caches()[0]


def reload_skills() -> None:
    """Drop the caches so the next call re-reads the ``.md`` files (tests/dev)."""
    global _CACHE, _COST_CACHE
    _CACHE = None
    _COST_CACHE = None


def _generic_type_instruction(attacker_type: str | None) -> str:
    """Build a fallback "use your domain" instruction from the type registry.

    Returns the domain description plus its signature move names, so even an
    unrecognised skill still nudges the debater toward type-appropriate tactics.
    "" when the type is unknown/missing.
    """
    if not attacker_type:
        return ""
    try:
        domain = domain_for_type(attacker_type)
    except Exception:  # noqa: BLE001
        return ""
    desc = (domain or {}).get("description", "")
    sigs = (domain or {}).get("signature_skills", []) or []
    if not desc and not sigs:
        return ""
    key = str(getattr(attacker_type, "value", attacker_type)).upper()
    parts = [f"Fight in your {key} domain."]
    if desc:
        parts.append(desc)
    if sigs:
        parts.append("Lean on signature moves like " + ", ".join(sigs) + ".")
    return " ".join(parts)


def skill_instructions(
    skill_name: str | None,
    attacker_type: str | None = None,
) -> str:
    """Return turn guidance for a debate move. NEVER raises.

    Resolution order:
      1. Exact slug match on ``skill_name`` against ``app/skills/<slug>.md``.
      2. Generic by-type ("domain") instruction built from ``attacker_type``.
      3. ``""`` when neither resolves.
    """
    try:
        slug = slugify(skill_name)
        if slug:
            cache = _load_cache()
            body = cache.get(slug)
            if body:
                return body
        return _generic_type_instruction(attacker_type)
    except Exception:  # noqa: BLE001 — guarantee: this function never raises
        return ""


# --------------------------------------------------------------------------- #
# Gacha wave: MP economy
# --------------------------------------------------------------------------- #


def skill_cost(skill_name: str | None) -> int:
    """Return the MP cost for a skill (parsed from front-matter ``mp_cost``).

    Unknown / empty / None -> 0 (free). Never raises. The orchestrator and the
    debate router both consult this to gate skill use (``current_mp >= cost``).
    """
    try:
        slug = slugify(skill_name)
        if not slug:
            return 0
        _, costs = _load_caches()
        return int(costs.get(slug, 0))
    except Exception:  # noqa: BLE001 — never raise
        return 0


def skill_costs() -> dict[str, int]:
    """Return a copy of ``{slug: mp_cost}`` for all loaded skill .md files.

    Used by the startup hook in ``app.main`` to bulk-update ``Skill.cost`` on
    every boot so the DB row mirrors the .md front-matter.
    """
    try:
        _, costs = _load_caches()
        return dict(costs)
    except Exception:  # noqa: BLE001 — never raise
        return {}
