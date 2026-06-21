"""Damage formula for the debate engine (WS-B).

Damage applied to a defender after the judge scores an utterance:

    base       = clamp(score - 50, 0, 50)          # only an above-average
                                                     # argument deals damage
    type_mult  = TYPE_CHART[attacker_type][defender_type]  (default 1.0)
    skill_mult = skill power (1.0 default)
    momentum   = side momentum multiplier (~0.8..1.3)
    level_scale= 1 + (attacker_level - defender_level) * 0.05

    damage = round(base * type_mult * skill_mult * momentum * level_scale)

TYPE_CHART mirrors packages/shared/enums.ts exactly (attacker -> defender ->
multiplier). Keep these two in sync by hand.
"""
from __future__ import annotations

import copy

# Neutral multiplier when a pairing is not listed in the chart.
NEUTRAL_MULTIPLIER: float = 1.0

# Default type chart — attacker -> defender -> multiplier. Mirrors
# packages/shared/enums.ts TYPE_CHART. Types are the DebateType *values*
# (uppercase strings) used everywhere else.
#
# This is the FROZEN default; battle math is unchanged unless the active chart is
# overridden (e.g. by reseeding from the Skill/type-chart catalog). Kept separate
# from the mutable `TYPE_CHART` so callers can always recover the shipped values
# via `reset_type_chart()`.
DEFAULT_TYPE_CHART: dict[str, dict[str, float]] = {
    "LOGOS": {"PATHOS": 1.5, "ETHOS": 0.75, "CHAOS": 0.75},
    "PATHOS": {"ETHOS": 1.5, "LOGOS": 0.75, "SOCRATIC": 0.75},
    "ETHOS": {"CHAOS": 1.5, "PATHOS": 0.75, "RHETORIC": 0.75},
    "CHAOS": {"LOGOS": 1.5, "RHETORIC": 1.5, "ETHOS": 0.75},
    "SOCRATIC": {"RHETORIC": 1.5, "PATHOS": 1.5, "LOGOS": 0.75},
    "RHETORIC": {"SOCRATIC": 0.75, "LOGOS": 1.5, "CHAOS": 0.75},
}

# The ACTIVE chart. Starts as an independent deep copy of the defaults so
# behavior is identical out of the box; can be overridden/extended at runtime
# (see set_type_chart / override_type_chart) without touching the defaults.
TYPE_CHART: dict[str, dict[str, float]] = copy.deepcopy(DEFAULT_TYPE_CHART)


def type_multiplier(attacker: str | None, defender: str | None) -> float:
    """Type-effectiveness multiplier; 1.0 when either type is unknown.

    Looks up the *active* :data:`TYPE_CHART`. Identical results to the original
    hardcoded chart unless the active chart has been overridden/extended.
    """
    if not attacker or not defender:
        return NEUTRAL_MULTIPLIER
    return TYPE_CHART.get(attacker.upper(), {}).get(
        defender.upper(), NEUTRAL_MULTIPLIER
    )


def set_type_chart(chart: dict[str, dict[str, float]]) -> None:
    """Replace the active type chart wholesale (keys normalized to uppercase).

    Use when a seed/catalog supplies the full chart. Pass an empty dict plus
    :func:`reset_type_chart` to restore defaults.
    """
    normalized: dict[str, dict[str, float]] = {}
    for attacker, row in chart.items():
        normalized[str(attacker).upper()] = {
            str(defender).upper(): float(mult) for defender, mult in row.items()
        }
    TYPE_CHART.clear()
    TYPE_CHART.update(normalized)


def override_type_chart(chart: dict[str, dict[str, float]]) -> None:
    """Merge entries into the active chart (per-pairing override/extend).

    Only the listed (attacker, defender) pairings change; everything else keeps
    its current value. Keys are normalized to uppercase.
    """
    for attacker, row in chart.items():
        dst = TYPE_CHART.setdefault(str(attacker).upper(), {})
        for defender, mult in row.items():
            dst[str(defender).upper()] = float(mult)


def reset_type_chart() -> None:
    """Restore the active chart to the frozen shipped defaults."""
    TYPE_CHART.clear()
    TYPE_CHART.update(copy.deepcopy(DEFAULT_TYPE_CHART))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_damage(
    score: float,
    attacker_type: str | None = None,
    defender_type: str | None = None,
    skill_mult: float = 1.0,
    momentum: float = 1.0,
    attacker_level: int = 1,
    defender_level: int = 1,
) -> int:
    """Return integer HP damage for one scored utterance.

    Only above-average arguments (score > 50) deal damage. The base is clamped
    to [0, 50] so a single perfect turn can deal at most ~50 * multipliers.
    """
    base = _clamp(score - 50.0, 0.0, 50.0)
    if base <= 0:
        return 0
    tmult = type_multiplier(attacker_type, defender_type)
    level_scale = 1.0 + (attacker_level - defender_level) * 0.05
    level_scale = _clamp(level_scale, 0.5, 2.0)
    raw = base * tmult * max(skill_mult, 0.0) * max(momentum, 0.0) * level_scale
    return max(0, round(raw))
