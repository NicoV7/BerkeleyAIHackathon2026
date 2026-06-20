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

# Attacker -> defender -> multiplier. Mirrors packages/shared/enums.ts TYPE_CHART.
# Types are the DebateType *values* (uppercase strings) used everywhere else.
TYPE_CHART: dict[str, dict[str, float]] = {
    "LOGOS": {"PATHOS": 1.5, "ETHOS": 0.75, "CHAOS": 0.75},
    "PATHOS": {"ETHOS": 1.5, "LOGOS": 0.75, "SOCRATIC": 0.75},
    "ETHOS": {"CHAOS": 1.5, "PATHOS": 0.75, "RHETORIC": 0.75},
    "CHAOS": {"LOGOS": 1.5, "RHETORIC": 1.5, "ETHOS": 0.75},
    "SOCRATIC": {"RHETORIC": 1.5, "PATHOS": 1.5, "LOGOS": 0.75},
    "RHETORIC": {"SOCRATIC": 0.75, "LOGOS": 1.5, "CHAOS": 0.75},
}


def type_multiplier(attacker: str | None, defender: str | None) -> float:
    """Type-effectiveness multiplier; 1.0 when either type is unknown."""
    if not attacker or not defender:
        return 1.0
    return TYPE_CHART.get(attacker.upper(), {}).get(defender.upper(), 1.0)


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
