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

from app.db.models import DebateType, Monster, MonsterOwner

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


def _pick_skills(rng: random.Random, debate_type: DebateType, n: int = 2) -> list[dict[str, Any]]:
    """Pick n skills, favouring the monster's own type, from the inline catalog."""
    same_type = [s for s in _SKILL_CATALOG if s["type"] == debate_type.value]
    other = [s for s in _SKILL_CATALOG if s["type"] != debate_type.value]
    chosen: list[dict[str, Any]] = []
    if same_type:
        chosen.append(rng.choice(same_type))
    while len(chosen) < n and other:
        pick = rng.choice(other)
        if pick not in chosen:
            chosen.append(pick)
    return chosen[:n]


def _build_persona(rng: random.Random) -> dict[str, Any]:
    return {
        "backstory": rng.choice(_BACKSTORIES),
        "tone": rng.choice(_TONES),
        "quirks": rng.choice(_QUIRKS),
    }


def _build_harness(persona: dict[str, Any], debate_type: DebateType) -> dict[str, Any]:
    system_prompt = (
        f"You are a debater of type {debate_type.value}. "
        f"Background: {persona['backstory']} "
        f"Tone: {persona['tone']}. "
        f"Quirk: {persona['quirks']}. "
        "Debate forcefully but fairly. Keep responses under 80 words."
    )
    return {"system_prompt": system_prompt}


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
            persona=persona,
            harness=_build_harness(persona, dtype),
            skills=skills,
            level=1,
            xp=0,
            max_hp=100,
            evolution_stage=0,
            model="gemma3:1b",
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
    session: AsyncSession, run_id: str, n: int = 4, seed: int = 0
) -> list[Monster]:
    """Create n wild enemy monsters and persist them to the DB.

    Exposed interface consumed by WS-B and WS-E.
    """
    rng = random.Random(seed ^ hash(run_id) & 0xFFFFFFFF ^ 0xDEAD)
    monsters: list[Monster] = []
    for i in range(n):
        dtype = rng.choice(_DEBATE_TYPES)
        name = rng.choice(_WILD_NAMES) + str(rng.randint(1, 99))
        persona = _build_persona(rng)
        skills = _pick_skills(rng, dtype, n=2)
        level = rng.randint(1, 5)
        m = Monster(
            run_id=run_id,
            owner=MonsterOwner.wild,
            name=name,
            type=dtype,
            persona=persona,
            harness=_build_harness(persona, dtype),
            skills=skills,
            level=level,
            xp=0,
            max_hp=80 + level * 10,
            evolution_stage=0,
            model="gemma3:1b",
            # Naive UTC to match TIMESTAMP WITHOUT TIME ZONE column
            created_at=datetime.utcnow(),
        )
        session.add(m)
        monsters.append(m)
    await session.commit()
    for m in monsters:
        await session.refresh(m)
    return monsters
