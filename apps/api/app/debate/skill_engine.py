"""Skill engine — loads rhetorical "skill.md" moves and exposes them as
turn-by-turn prompt guidance for the debate orchestrator.

VISION: each command (e.g. "Credential Drop") is a real ``skill.md`` the agent
executes as a turn to turn the debate in its favour. This module loads those
markdown move files from ``app/skills/`` (one per move), caches them, and serves
the move's instruction text on demand.

Public surface (kept STABLE — the orchestrator imports this directly):

    skill_instructions(skill_name: str | None, attacker_type: str | None = None) -> str

Behaviour:
  * Slug-match ``skill_name`` against ``app/skills/<slug>.md`` and return the
    move's instruction body (front-matter stripped).
  * If the named skill is unknown but ``attacker_type`` is given, fall back to a
    generic by-type ("domain") instruction built from the type's signature
    moves.
  * Return ``""`` (never raise) when nothing matches — the orchestrator treats an
    empty string as "no extra guidance".

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


def slugify(name: str | None) -> str:
    """Normalise a skill display name to a filename slug.

    "Credential Drop" -> "credential_drop". Non-alphanumerics collapse to single
    underscores; result is lower-cased and trimmed. Empty/None -> "".
    """
    if not name:
        return ""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return s.strip("_")


def _strip_front_matter(text: str) -> str:
    """Remove a leading ``---`` YAML front-matter block, if present."""
    if text.startswith("---"):
        # Split on the closing fence; tolerate CRLF.
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def _load_cache() -> dict[str, str]:
    """Read every ``app/skills/*.md`` once and cache slug -> instruction text."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cache: dict[str, str] = {}
    try:
        if _SKILLS_DIR.is_dir():
            for path in sorted(_SKILLS_DIR.glob("*.md")):
                try:
                    raw = path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001 — skip unreadable file, keep going
                    continue
                body = _strip_front_matter(raw)
                if body:
                    cache[path.stem.lower()] = body
    except Exception:  # noqa: BLE001 — directory iteration failure -> empty cache
        cache = {}
    _CACHE = cache
    return _CACHE


def reload_skills() -> None:
    """Drop the cache so the next call re-reads the ``.md`` files (tests/dev)."""
    global _CACHE
    _CACHE = None


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
