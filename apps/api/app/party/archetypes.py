"""Debater persona archetypes (Agent 6: OPPONENT VARIETY).

A pure-data catalog of distinct wild-debater *archetypes* so the overworld is
populated by recognisably different opponents instead of flat, interchangeable
enemies. Each archetype carries:

  * ``key``         — stable identifier (handy for tests / telemetry).
  * ``name``        — the display title ("The Zealot", ...).
  * ``tone``        — voice descriptor fed into the harness prompt.
  * ``quirk``       — a behavioural tic for flavour.
  * ``backstory``   — one-line lore.
  * ``type_bias``   — ordered list of ``DebateType`` values this archetype
                      prefers (first = primary element). Drives the spawned
                      monster's ``type`` and skill selection.
  * ``power_bias``  — multiplicative nudge on skill power (1.0 = neutral) so the
                      heavier-hitting archetypes feel a little spicier without
                      breaking the level/HP curve owned by ``balance``.

This module is **pure data + one seeded helper** with NO I/O — safe to import
anywhere (generator, tests, balancing notebooks).
"""
from __future__ import annotations

import random
from typing import Any

from app.db.models import DebateType

# ---------------------------------------------------------------------------
# Archetype catalog
# ---------------------------------------------------------------------------

ARCHETYPES: list[dict[str, Any]] = [
    {
        "key": "zealot",
        "name": "The Zealot",
        "tone": "fervent",
        "quirk": "treats every premise as a moral crusade",
        "backstory": "A true believer who never met a cause too small to die on.",
        "type_bias": [DebateType.pathos, DebateType.ethos],
        "power_bias": 1.15,
    },
    {
        "key": "sophist",
        "name": "The Sophist",
        "tone": "slippery",
        "quirk": "can argue any side with equal conviction",
        "backstory": "A hired tongue who sells certainty by the syllable.",
        "type_bias": [DebateType.rhetoric, DebateType.chaos],
        "power_bias": 1.05,
    },
    {
        "key": "pedant",
        "name": "The Pedant",
        "tone": "fussy",
        "quirk": "derails on definitions and footnotes",
        "backstory": "A walking errata sheet who corrects your grammar mid-point.",
        "type_bias": [DebateType.logos, DebateType.socratic],
        "power_bias": 0.95,
    },
    {
        "key": "contrarian",
        "name": "The Contrarian",
        "tone": "combative",
        "quirk": "reflexively takes the opposite position",
        "backstory": "Allergic to consensus; thrives on being the lone dissenter.",
        "type_bias": [DebateType.chaos, DebateType.rhetoric],
        "power_bias": 1.1,
    },
    {
        "key": "empath",
        "name": "The Empath",
        "tone": "earnest",
        "quirk": "reframes every clash as a shared feeling",
        "backstory": "Disarms opponents with relentless, genuine warmth.",
        "type_bias": [DebateType.pathos, DebateType.socratic],
        "power_bias": 0.9,
    },
    {
        "key": "technocrat",
        "name": "The Technocrat",
        "tone": "clinical",
        "quirk": "answers everything with a chart and a citation",
        "backstory": "A data-driven optimiser who distrusts anything unquantified.",
        "type_bias": [DebateType.logos, DebateType.ethos],
        "power_bias": 1.0,
    },
    {
        "key": "demagogue",
        "name": "The Demagogue",
        "tone": "rousing",
        "quirk": "plays to the crowd over the argument",
        "backstory": "A born performer who wins rooms before winning points.",
        "type_bias": [DebateType.rhetoric, DebateType.pathos],
        "power_bias": 1.2,
    },
    {
        "key": "inquisitor",
        "name": "The Inquisitor",
        "tone": "probing",
        "quirk": "answers questions only with sharper questions",
        "backstory": "A relentless interrogator who corners you with your own words.",
        "type_bias": [DebateType.socratic, DebateType.logos],
        "power_bias": 1.05,
    },
    {
        "key": "trickster",
        "name": "The Trickster",
        "tone": "whimsical",
        "quirk": "delights in absurd reframes and reversals",
        "backstory": "A chaos-loving jester who weaponises the unexpected.",
        "type_bias": [DebateType.chaos, DebateType.rhetoric],
        "power_bias": 1.1,
    },
    {
        "key": "elder",
        "name": "The Elder Statesman",
        "tone": "measured",
        "quirk": "anchors every point in hard-won precedent",
        "backstory": "A veteran of a thousand debates who has seen it all before.",
        "type_bias": [DebateType.ethos, DebateType.logos],
        "power_bias": 1.0,
    },
]

#: Quick lookup by key.
ARCHETYPES_BY_KEY: dict[str, dict[str, Any]] = {a["key"]: a for a in ARCHETYPES}


# ---------------------------------------------------------------------------
# Types as DOMAINS: each DebateType is a rhetorical domain with its own
# description + signature skill moves. This is the single source of truth that
# the generator (to assign type-appropriate skills) and the skill_engine (to
# fall back to a by-type instruction) both read.
#
# Skill NAMES here are kept identical to the inline catalog in
# ``app.party.generator`` and to packages/shared/enums.ts so frontend chips,
# damage typing, and the skill .md files all line up. Each name slugifies to a
# matching ``app/skills/<slug>.md`` file consumed by the skill engine.
# ---------------------------------------------------------------------------

#: DebateType value (uppercase string) -> domain definition.
#:   * ``description``  — what this rhetorical domain is about.
#:   * ``signature_skills`` — the move names that belong to this domain.
TYPE_DOMAINS: dict[str, dict[str, Any]] = {
    "LOGOS": {
        "description": (
            "The domain of logic and evidence. LOGOS debaters win with facts, "
            "tight reasoning, data, and structural rigour."
        ),
        "signature_skills": ["Contradiction Ledger", "Evidence Echo"],
    },
    "PATHOS": {
        "description": (
            "The domain of emotion and story. PATHOS debaters move the audience "
            "with vivid human stakes, empathy, and felt consequence."
        ),
        "signature_skills": ["Wound Callback", "Empathy Mirror"],
    },
    "ETHOS": {
        "description": (
            "The domain of credibility and authority. ETHOS debaters borrow trust "
            "from expertise, institutions, and earned standing."
        ),
        "signature_skills": ["Reputation Crosscheck", "Authority Reversal"],
    },
    "CHAOS": {
        "description": (
            "The domain of disruption and reframing. CHAOS debaters refuse the "
            "given frame, flip premises, and scramble the opponent's line."
        ),
        "signature_skills": ["Pattern Break", "Hypocrisy Hook"],
    },
    "SOCRATIC": {
        "description": (
            "The domain of questioning. SOCRATIC debaters win by asking precise "
            "questions that expose gaps and lead opponents to concede."
        ),
        "signature_skills": ["Memory Trap", "Premise Recall"],
    },
    "RHETORIC": {
        "description": (
            "The domain of style and framing. RHETORIC debaters persuade with "
            "memorable phrasing, vivid analogy, and rhythmic force."
        ),
        "signature_skills": ["Callback Crescendo", "Phrase Reversal"],
    },
}


def domain_for_type(debate_type: Any) -> dict[str, Any]:
    """Return the domain definition for a ``DebateType`` or its string value.

    Accepts a :class:`DebateType` enum member or a (case-insensitive) string.
    Returns an empty-ish default ``{"description": "", "signature_skills": []}``
    for an unknown type so callers never need to guard against ``KeyError``.
    """
    key = getattr(debate_type, "value", debate_type)
    if key is None:
        return {"description": "", "signature_skills": []}
    return TYPE_DOMAINS.get(str(key).upper(), {"description": "", "signature_skills": []})


def signature_skills_for_type(debate_type: Any) -> list[str]:
    """Return the list of signature skill names for a type (``[]`` if unknown)."""
    return list(domain_for_type(debate_type).get("signature_skills", []))


def pick_archetype(rng: random.Random) -> dict[str, Any]:
    """Return one archetype dict, chosen with the provided seeded RNG.

    Deterministic for a given ``rng`` state, so callers that thread a seeded
    ``random.Random`` get reproducible opponents.
    """
    return rng.choice(ARCHETYPES)


def persona_for(archetype: dict[str, Any]) -> dict[str, Any]:
    """Build a ``Monster.persona`` dict from an archetype.

    Mirrors the persona shape the generator already used (``backstory`` /
    ``tone`` / ``quirks``) and tags the source archetype for downstream flavour.
    """
    return {
        "archetype": archetype["name"],
        "archetype_key": archetype["key"],
        "backstory": archetype["backstory"],
        "tone": archetype["tone"],
        "quirks": archetype["quirk"],
    }
