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
