"""Party and wild-enemy generator for WS-A.

Exposed interfaces (imported by WS-B and WS-E):
    roll_starter_party(session, run_id) -> list[Monster]
    generate_wild(session, run_id, n, seed=0) -> list[Monster]
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import DebateType, Monster, MonsterOwner
from app.party import archetypes as _archetypes
from app.party import balance as _balance
from app.party import persona as _persona

# ---------------------------------------------------------------------------
# Inline skill catalog (tiny seed list — global catalog seeded in Wave 2)
# ---------------------------------------------------------------------------

_SKILL_CATALOG: list[dict[str, Any]] = [
    {"name": "Logical Thrust",    "type": "LOGOS",    "power": 1.2, "description": "Present a well-sourced fact."},
    {"name": "Emotional Appeal",  "type": "PATHOS",   "power": 1.2, "description": "Share a moving personal story."},
    {"name": "Authority Cite",    "type": "ETHOS",    "power": 1.1, "description": "Invoke expert consensus."},
    {"name": "Reframe Attack",    "type": "CHAOS",    "power": 1.3, "description": "Flip the opponent's premise."},
    {"name": "Socratic Probe",    "type": "SOCRATIC", "power": 1.0, "description": "Ask a probing question."},
    {"name": "Rhetorical Flourish","type": "RHETORIC","power": 1.1, "description": "Deploy stylistic persuasion."},
    {"name": "Steel Man",         "type": "LOGOS",    "power": 0.9, "description": "Acknowledge opposing strength."},
    {"name": "Anecdote",          "type": "PATHOS",   "power": 0.8, "description": "Tell a relatable story."},
    {"name": "Credential Drop",   "type": "ETHOS",    "power": 1.0, "description": "Establish personal authority."},
    {"name": "Whataboutism",      "type": "CHAOS",    "power": 0.7, "description": "Deflect with a counter-example."},
    {"name": "Leading Question",  "type": "SOCRATIC", "power": 1.1, "description": "Guide toward your conclusion."},
    {"name": "Analogy Strike",    "type": "RHETORIC", "power": 1.0, "description": "Compare with vivid analogy."},
]

#: name -> catalog dict, for resolving a domain's signature skill names back to
#: full skill objects (with type/power/description) when generating monsters.
_SKILL_BY_NAME: dict[str, dict[str, Any]] = {s["name"]: s for s in _SKILL_CATALOG}

# Persona templates
_BACKSTORIES = [
    "A philosophy professor who debates for sport.",
    "A street activist who learned rhetoric the hard way.",
    "An AI trained on a million debate transcripts.",
    "A retired lawyer who never lost a case.",
    "A charismatic politician with silver-tongued wit.",
    "An eccentric inventor who argues through examples.",
    "A journalist who traffics in provocative questions.",
    "A scientist who weaponises data like a scalpel.",
]

_TONES = ["assertive", "sardonic", "earnest", "combative", "measured", "whimsical", "relentless"]
_QUIRKS = [
    "always opens with a historical anecdote",
    "speaks in rhetorical questions",
    "loves Latin phrases",
    "quotes popular culture constantly",
    "pauses dramatically before key points",
    "uses sports metaphors",
    "references obscure academic papers",
    "turns every point into a three-step argument",
]

_DEBATE_TYPES = list(DebateType)

_STARTER_NAMES = [
    "LogiKnight", "PathosDrake", "EthosGuard", "ChaosWitch",
    "SocraLeaf", "RhetorFox", "ArgMaster", "DialectiCat",
]
_WILD_NAMES = [
    "Stumpit", "Blustero", "Dogmatix", "Quibblon", "Snarkle",
    "Fallacitor", "Moot", "Hedgeling", "Pedantus", "Contrarian",
    "Verbosio", "Demagog", "Sophist", "Hyperbole", "Truism",
]


def _domain_skills_for(debate_type: DebateType) -> list[dict[str, Any]]:
    """Resolve a type's domain *signature* skills to full catalog dicts.

    Reads the type->domain->skills registry in :mod:`app.party.archetypes`
    (single source of truth), then maps each signature move name back to its
    full ``{name, type, power, description}`` object. Falls back to type-matched
    catalog entries if the registry is unavailable, so generation never breaks.
    """
    names = _archetypes.signature_skills_for_type(debate_type)
    skills = [_SKILL_BY_NAME[n] for n in names if n in _SKILL_BY_NAME]
    if not skills:
        skills = [s for s in _SKILL_CATALOG if s["type"] == debate_type.value]
    return skills


def _pick_skills(rng: random.Random, debate_type: DebateType, n: int = 2) -> list[dict[str, Any]]:
    """Pick n skills drawn from the monster's TYPE domain first.

    A monster gets moves from its own rhetorical domain (a PATHOS agent gets
    pathos moves), per the type->domain registry, topped up from other domains
    only if more are needed. Backward-compatible: same return shape (list of
    catalog dicts), still favours the monster's own type, deterministic for a
    given ``rng``.
    """
    same_type = _domain_skills_for(debate_type)
    other = [s for s in _SKILL_CATALOG if s["type"] != debate_type.value]
    chosen: list[dict[str, Any]] = []
    if same_type:
        chosen.append(rng.choice(same_type))
    # Prefer remaining same-domain moves before reaching into other domains.
    remaining_same = [s for s in same_type if s not in chosen]
    pools = (remaining_same, other)
    for pool in pools:
        pool = list(pool)
        while len(chosen) < n and pool:
            pick = rng.choice(pool)
            pool.remove(pick)
            if pick not in chosen:
                chosen.append(pick)
        if len(chosen) >= n:
            break
    return chosen[:n]


def _build_persona(rng: random.Random) -> dict[str, Any]:
    return {
        "backstory": rng.choice(_BACKSTORIES),
        "tone": rng.choice(_TONES),
        "quirks": rng.choice(_QUIRKS),
    }


def _build_harness(
    persona: dict[str, Any],
    debate_type: DebateType,
    *,
    role: str = "party",
) -> dict[str, Any]:
    system_prompt = (
        f"Thin {role} battle harness for {debate_type.value}: obey role, stance, and output contract."
    )
    domain = _archetypes.domain_for_type(debate_type)
    fragments = []
    if domain.get("description"):
        fragments.append(str(domain["description"]))
    if role == "enemy":
        fragments.append(
            "Open by rebutting the party's latest claim, then add one concrete failure mode."
        )
    else:
        fragments.append(
            "Coordinate with the party's current argument and avoid repeating it."
        )
    return _persona.normalize_harness(
        {
            "system_prompt": system_prompt,
            "skill_prompt_fragments": fragments,
        },
        role=role,
    )


# ---------------------------------------------------------------------------
# Difficulty curve (depth/progress -> stronger wild enemies)
# ---------------------------------------------------------------------------
#
# "depth"/"progress" = how far the player has come (encounters cleared / distance
# from start). At depth 0 we reproduce the original behaviour exactly. Deeper
# encounters spawn higher-level, higher-HP foes. All HP/level math defers to
# ``app.party.balance`` so the curve stays single-sourced and rebalanceable.

#: Base wild level band at depth 0 — matches the original ``randint(1, 5)``.
_WILD_BASE_LEVEL_MIN = 1
_WILD_BASE_LEVEL_MAX = 5

#: Extra levels added per unit of depth (every ~2 encounters cleared = +1 level).
_LEVELS_PER_DEPTH = 0.5


def _wild_level_for_depth(rng: random.Random, depth: int) -> int:
    """Pick a wild enemy level scaled by ``depth``.

    At ``depth == 0`` this draws from ``[1, 5]`` exactly like the original
    generator (and consumes the rng identically). Deeper encounters shift the
    whole band upward, so average level rises monotonically with depth.
    """
    base = rng.randint(_WILD_BASE_LEVEL_MIN, _WILD_BASE_LEVEL_MAX)
    bonus = int(max(0, depth) * _LEVELS_PER_DEPTH)
    return base + bonus


def _scale_skill_power(skills: list[dict[str, Any]], power_bias: float) -> list[dict[str, Any]]:
    """Return copies of ``skills`` with their ``power`` nudged by ``power_bias``.

    Leaves the shared catalog dicts untouched (defensive copy) and rounds to keep
    the values tidy. A bias of 1.0 is a no-op except for the copy.
    """
    scaled: list[dict[str, Any]] = []
    for s in skills:
        s2 = dict(s)
        if "power" in s2 and isinstance(s2["power"], (int, float)):
            s2["power"] = round(float(s2["power"]) * power_bias, 3)
        scaled.append(s2)
    return scaled


def build_wild_monster(
    rng: random.Random,
    run_id: str,
    *,
    depth: int = 0,
) -> Monster:
    """Construct (but do NOT persist) a single wild enemy ``Monster``.

    Pure factory: deterministic for a given ``rng`` state + ``depth``, with no
    DB/Redis/network. ``generate_wild`` wraps this to add session persistence.

    * Picks an :mod:`app.party.archetypes` archetype for persona + type bias.
    * Levels/HP scale with ``depth`` via :func:`_wild_level_for_depth` and
      :func:`app.party.balance.hp_for_level` (single-sourced curve).
    * At ``depth == 0`` the level band and HP match the original generator.
    """
    archetype = _archetypes.pick_archetype(rng)
    # Primary element comes from the archetype's bias (first preference).
    dtype = archetype["type_bias"][0]
    name = rng.choice(_WILD_NAMES) + str(rng.randint(1, 99))
    persona = _archetypes.persona_for(archetype)
    skills = _pick_skills(rng, dtype, n=2)
    skills = _scale_skill_power(skills, archetype["power_bias"])
    level = _wild_level_for_depth(rng, depth)
    evolution_stage = _balance.evolution_stage_for_level(level)
    max_hp = _balance.hp_for_level(level, evolution_stage=evolution_stage)
    return Monster(
        run_id=run_id,
        owner=MonsterOwner.wild,
        name=name,
        type=dtype,
        persona=_persona.ensure_battle_reactions(
            persona,
            dtype,
            role="enemy",
            fallback_name=name,
        ),
        harness=_build_harness(persona, dtype, role="enemy"),
        skills=skills,
        level=level,
        xp=0,
        max_hp=max_hp,
        evolution_stage=evolution_stage,
        model=settings.actor_model,
        # Naive UTC to match TIMESTAMP WITHOUT TIME ZONE column
        created_at=datetime.utcnow(),
    )


async def roll_starter_party(session: AsyncSession, run_id: str, seed: int = 0) -> list[Monster]:
    """Create 2–3 starter party monsters and persist them to the DB.

    Exposed interface consumed by WS-B and WS-E.
    """
    rng = random.Random(seed ^ hash(run_id) & 0xFFFFFFFF)
    n = rng.randint(2, 3)
    monsters: list[Monster] = []
    used_names: set[str] = set()
    for _ in range(n):
        dtype = rng.choice(_DEBATE_TYPES)
        name = rng.choice([nm for nm in _STARTER_NAMES if nm not in used_names] or _STARTER_NAMES)
        used_names.add(name)
        persona = _build_persona(rng)
        skills = _pick_skills(rng, dtype, n=2)
        m = Monster(
            run_id=run_id,
            owner=MonsterOwner.player,
            name=name,
            type=dtype,
            persona=_persona.ensure_battle_reactions(
                persona,
                dtype,
                role="party",
                fallback_name=name,
            ),
            harness=_build_harness(persona, dtype, role="party"),
            skills=skills,
            level=1,
            xp=0,
            max_hp=100,
            evolution_stage=0,
            model=settings.actor_model,
            # Naive UTC to match TIMESTAMP WITHOUT TIME ZONE column
            created_at=datetime.utcnow(),
        )
        session.add(m)
        monsters.append(m)
    await session.commit()
    for m in monsters:
        await session.refresh(m)
    return monsters


async def generate_wild(
    session: AsyncSession,
    run_id: str,
    n: int = 4,
    seed: int = 0,
    *,
    depth: int = 0,
    progress: int = 0,
) -> list[Monster]:
    """Create n wild enemy monsters and persist them to the DB.

    Exposed interface consumed by WS-B and WS-E.

    Difficulty curve (additive, backward-compatible):
      * ``depth`` / ``progress`` express how far the player has come (encounters
        cleared / distance from start). They are interchangeable; the larger of
        the two is used. Higher values spawn higher-level, higher-HP foes, with
        the level/HP curve sourced from :mod:`app.party.balance`.
      * Each enemy draws a flavour archetype from :mod:`app.party.archetypes`,
        which also biases its debate type and skill power.

    Determinism is preserved via the existing seed/run_id derived rng. At
    ``depth == 0`` (and ``progress == 0``) the level band matches the original
    generator; HP is now taken from the canonical ``balance.hp_for_level`` curve.
    """
    effective_depth = max(0, int(depth), int(progress))
    rng = random.Random(seed ^ hash(run_id) & 0xFFFFFFFF ^ 0xDEAD)
    monsters: list[Monster] = []
    for _ in range(n):
        m = build_wild_monster(rng, run_id, depth=effective_depth)
        session.add(m)
        monsters.append(m)
    await session.commit()
    for m in monsters:
        await session.refresh(m)
    return monsters
