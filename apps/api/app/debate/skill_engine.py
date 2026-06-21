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
from typing import Any

try:  # type/domain registry lives with the archetypes (single source of truth)
    from app.party.archetypes import TYPE_DOMAINS, domain_for_type
except Exception:  # noqa: BLE001 — never let an import problem break the engine
    TYPE_DOMAINS = {}  # type: ignore[assignment]

    def domain_for_type(_dt):  # type: ignore[misc]
        return {"description": "", "signature_skills": []}


#: Directory holding the per-move ``.md`` files (sibling ``app/skills``).
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

#: slug -> parsed skill metadata + prompt body. Populated lazily, once.
_SPEC_CACHE: dict[str, dict[str, Any]] | None = None

#: Back-compat caches for the original public helpers.
_CACHE: dict[str, str] | None = None
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


_VALID_EFFECT_KINDS = {
    "agent_argument",
    "prompt_augment",
    "defense",
    "status",
    "intel_preview",
    "judge_sway",
}

_VALID_RARITIES = {"common", "rare", "legendary"}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_front_matter(front_matter: str) -> dict[str, str]:
    """Parse the simple scalar frontmatter shape used by battle skill files."""
    out: dict[str, str] = {}
    for raw in (front_matter or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            out[key] = value
    return out


def _parse_modifiers(front: dict[str, str]) -> dict[str, Any]:
    """Return effect modifiers from a compact ``modifiers`` string plus aliases.

    The catalog intentionally avoids a YAML dependency. Authors can write
    ``modifiers: damage_mult=1.2,score_delta=4`` and common top-level aliases
    such as ``score_delta`` or ``enemy_sentence_limit`` are folded in too.
    """
    modifiers: dict[str, Any] = {}
    raw = front.get("modifiers", "")
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            modifiers[key] = _coerce_modifier_value(value)
    for key in (
        "damage_mult",
        "score_delta",
        "defense_mult",
        "enemy_sentence_limit",
        "enemy_max_tokens",
        "prompt_bonus",
        "judge_reason",
        "angle",
    ):
        if key in front and key not in modifiers:
            modifiers[key] = _coerce_modifier_value(front[key])
    return modifiers


def _coerce_modifier_value(value: str) -> Any:
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value.strip("\"'")


def _body_summary(body: str) -> str:
    """Extract a compact UI description from a skill body when none is declared."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if line.startswith("**Move type") or line.startswith("**Cost"):
            continue
        return line[:180]
    return ""


def _spec_from_file(path: Path, raw: str) -> tuple[str, dict[str, Any]] | None:
    front_raw, body = _split_front_matter(raw)
    if not body:
        return None
    front = _parse_front_matter(front_raw)
    name = front.get("name") or path.stem.replace("_", " ").title()
    slug = slugify(name) or path.stem.lower()
    effect_kind = str(front.get("effect_kind") or front.get("special") or "agent_argument")
    if effect_kind == "redis_memory_attack":
        effect_kind = "agent_argument"
    if effect_kind == "redis_peek":
        effect_kind = "agent_argument"
    if effect_kind not in _VALID_EFFECT_KINDS:
        effect_kind = "agent_argument"
    rarity = str(front.get("rarity") or "common").lower()
    if rarity not in _VALID_RARITIES:
        rarity = "common"
    spec = {
        "id": slug,
        "slug": slug,
        "name": name,
        "type": str(front.get("type") or front.get("domain") or "").upper(),
        "domain": str(front.get("domain") or front.get("type") or "").upper(),
        "power": _parse_float(front.get("power"), 1.0),
        "description": front.get("description") or _body_summary(body),
        "prompt_fragment": body,
        "mp_cost": max(0, _parse_int(front.get("mp_cost"), 0)),
        "cost": max(0, _parse_int(front.get("mp_cost"), 0)),
        "effect_kind": effect_kind,
        "target": str(front.get("target") or "enemy"),
        "duration_turns": max(0, _parse_int(front.get("duration_turns"), 0)),
        "requires_prompt": _parse_bool(
            front.get("requires_prompt"),
            default=effect_kind in {"prompt_augment", "judge_sway", "defense", "status"},
        ),
        "rarity": rarity,
        "modifiers": _parse_modifiers(front),
    }
    return slug, spec


def _load_caches() -> tuple[dict[str, str], dict[str, int]]:
    """Read every ``app/skills/*.md`` once; cache specs, bodies, and costs."""
    global _CACHE, _COST_CACHE, _SPEC_CACHE
    if _CACHE is not None and _COST_CACHE is not None and _SPEC_CACHE is not None:
        return _CACHE, _COST_CACHE
    body_cache: dict[str, str] = {}
    cost_cache: dict[str, int] = {}
    spec_cache: dict[str, dict[str, Any]] = {}
    try:
        if _SKILLS_DIR.is_dir():
            for path in sorted(_SKILLS_DIR.glob("*.md")):
                try:
                    raw = path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001 — skip unreadable file, keep going
                    continue
                parsed = _spec_from_file(path, raw)
                if parsed is None:
                    continue
                slug, spec = parsed
                body_cache[slug] = str(spec.get("prompt_fragment") or "")
                cost_cache[slug] = int(spec.get("mp_cost", 0) or 0)
                spec_cache[slug] = spec
    except Exception:  # noqa: BLE001 — directory iteration failure -> empty caches
        body_cache = {}
        cost_cache = {}
        spec_cache = {}
    _CACHE = body_cache
    _COST_CACHE = cost_cache
    _SPEC_CACHE = spec_cache
    return _CACHE, _COST_CACHE


def _load_cache() -> dict[str, str]:
    """Back-compat alias for the body-only cache used by ``skill_instructions``."""
    return _load_caches()[0]


def reload_skills() -> None:
    """Drop the caches so the next call re-reads the ``.md`` files (tests/dev)."""
    global _CACHE, _COST_CACHE, _SPEC_CACHE
    _CACHE = None
    _COST_CACHE = None
    _SPEC_CACHE = None


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


def skill_metadata(skill_name: str | None) -> dict[str, Any]:
    """Return a copy of one skill's parsed catalog metadata, or ``{}``.

    The returned dict is safe for callers to mutate. It is intentionally flat and
    JSON-serializable so it can be stored directly on ``Monster.skills``.
    """
    try:
        slug = slugify(skill_name)
        if not slug:
            return {}
        _load_caches()
        spec = (_SPEC_CACHE or {}).get(slug)
        if not spec:
            return {}
        return dict(spec)
    except Exception:  # noqa: BLE001
        return {}


def skill_catalog() -> list[dict[str, Any]]:
    """Return the full battle skill catalog parsed from ``app/skills/*.md``."""
    try:
        _load_caches()
        return [dict(s) for s in (_SPEC_CACHE or {}).values()]
    except Exception:  # noqa: BLE001
        return []


def skills_for_type(debate_type: Any) -> list[dict[str, Any]]:
    """Return catalog skills whose ``type`` matches a DebateType or string."""
    key = str(getattr(debate_type, "value", debate_type) or "").upper()
    if not key:
        return []
    return [s for s in skill_catalog() if str(s.get("type", "")).upper() == key]
