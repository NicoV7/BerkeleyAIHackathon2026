"""FF12-style Gambit rule engine.

This module evaluates a monster's ordered gambit rules against the current
battle state and returns the first matching action, or a default fallback.

Expected ``battle_state`` keys (WS-B passes these when calling choose_action):
  hp           : dict[str, int]   — current HP for every combatant keyed by monster id
  max_hp       : dict[str, int]   — max HP for every combatant keyed by monster id
  last_verdict_score : float      — judge score from the most recent JudgeVerdict
                                    (positive = party doing well, negative = enemy doing well)
  turn_no      : int              — 0-indexed turn counter
  topic        : str              — full debate topic string for keyword matching
  momentum     : dict[str, float] — cumulative momentum per side ("party"/"enemy")
  self_id      : str              — this monster's id
  ally_ids     : list[str]        — ids of friendly combatants (same side, excluding self)
  enemy_ids    : list[str]        — ids of opposing combatants

Condition DSL (GambitRuleModel.condition):
  {kind, op, value}
  kind  ∈ {self_hp_pct, ally_hp_pct, enemy_hp_pct, last_verdict_score,
            turn_no, topic_keyword, momentum}
  op    ∈ {<, <=, >, >=, ==, contains}
  value : number or str depending on kind

  * self_hp_pct   — self current HP / max_hp * 100
  * ally_hp_pct   — lowest ally HP as a percentage (or 100 if no allies)
  * enemy_hp_pct  — lowest enemy HP as a percentage (or 100 if no enemies)
  * last_verdict_score — last judge score float
  * turn_no       — current turn number
  * topic_keyword — op must be "contains"; value is a substring to find in topic
  * momentum      — momentum["party"] - momentum["enemy"] (positive = party winning)

Action DSL (GambitRuleModel.action):
  {kind, ...}
  kind ∈ {use_skill, target, tone, default}
  * use_skill  — also has "skill_id": str
  * target     — also has "who": "lowest_hp_enemy" | "highest_hp_enemy" | specific id
  * tone       — also has "value": str (e.g. "aggressive", "defensive", "sarcastic")
  * default    — no extra fields; the fallback

Rules are evaluated in ascending priority order; first match wins.
If no rule matches, returns {"kind": "default"}.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

_OPS: dict[str, Any] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
}


def _hp_pct(hp_map: dict[str, int], max_map: dict[str, int], mid: str) -> float:
    """Return HP% for a single monster id (0-100). Returns 100 if not found."""
    cur = hp_map.get(mid)
    mx = max_map.get(mid)
    if cur is None or mx is None or mx == 0:
        return 100.0
    return (cur / mx) * 100.0


def _evaluate_condition(condition: dict[str, Any], battle_state: dict[str, Any]) -> bool:
    """Return True when the condition is satisfied against the battle state."""
    if not condition:
        # empty condition → always true (unconditional gambit)
        return True

    kind: str = condition.get("kind", "")
    op: str = condition.get("op", "")
    value: Any = condition.get("value")

    op_fn = _OPS.get(op)
    if op_fn is None:
        log.warning("Unknown gambit op '%s' — skipping rule", op)
        return False

    hp = battle_state.get("hp", {})
    max_hp = battle_state.get("max_hp", {})
    self_id: str = battle_state.get("self_id", "")
    ally_ids: list[str] = battle_state.get("ally_ids", [])
    enemy_ids: list[str] = battle_state.get("enemy_ids", [])
    topic: str = battle_state.get("topic", "")
    momentum: dict[str, float] = battle_state.get("momentum", {})

    if kind == "self_hp_pct":
        lhs = _hp_pct(hp, max_hp, self_id)
        return op_fn(lhs, float(value))

    elif kind == "ally_hp_pct":
        if not ally_ids:
            lhs = 100.0
        else:
            lhs = min(_hp_pct(hp, max_hp, aid) for aid in ally_ids)
        return op_fn(lhs, float(value))

    elif kind == "enemy_hp_pct":
        if not enemy_ids:
            lhs = 100.0
        else:
            lhs = min(_hp_pct(hp, max_hp, eid) for eid in enemy_ids)
        return op_fn(lhs, float(value))

    elif kind == "last_verdict_score":
        lhs = float(battle_state.get("last_verdict_score", 0.0))
        return op_fn(lhs, float(value))

    elif kind == "turn_no":
        lhs = int(battle_state.get("turn_no", 0))
        return op_fn(lhs, int(value))

    elif kind == "topic_keyword":
        # op should be "contains"; lhs is the topic string
        return op_fn(topic, value)

    elif kind == "momentum":
        party_mom = float(momentum.get("party", 0.0))
        enemy_mom = float(momentum.get("enemy", 0.0))
        lhs = party_mom - enemy_mom
        return op_fn(lhs, float(value))

    else:
        log.warning("Unknown gambit condition kind '%s' — skipping", kind)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def choose_action(monster: Any, battle_state: dict[str, Any]) -> dict[str, Any]:
    """Choose the first matching gambit action for *monster* given *battle_state*.

    Parameters
    ----------
    monster:
        A db.models.Monster ORM instance (or duck-typed object) that exposes a
        ``skills`` attribute.  We read ``monster.id`` to seed ``self_id`` in
        battle_state when it isn't already set.
    battle_state:
        See module docstring for the full key contract.

    Returns
    -------
    dict with at least a ``"kind"`` key.  Falls back to ``{"kind": "default"}``
    when no rule matches or the gambit list is empty.
    """
    # Defensive: if the module is imported but monster has no gambit attribute
    # (older model version) just return default.
    gambit_rules: list[Any] = []
    try:
        gambit_rules = list(getattr(monster, "_gambits", []) or [])
    except Exception:
        pass

    if not gambit_rules:
        return {"kind": "default"}

    # Ensure self_id is in battle_state
    if "self_id" not in battle_state:
        try:
            battle_state = dict(battle_state, self_id=str(monster.id))
        except Exception:
            pass

    # Sort enabled rules by ascending priority then evaluate
    enabled = sorted(
        [r for r in gambit_rules if getattr(r, "enabled", True)],
        key=lambda r: getattr(r, "priority", 0),
    )

    for rule in enabled:
        condition = getattr(rule, "condition", {}) or {}
        action = getattr(rule, "action", {}) or {}
        try:
            if _evaluate_condition(condition, battle_state):
                log.debug(
                    "Gambit matched: priority=%s condition=%s -> action=%s",
                    getattr(rule, "priority", 0),
                    condition,
                    action,
                )
                return dict(action) if action else {"kind": "default"}
        except Exception as exc:
            log.warning("Error evaluating gambit rule: %s", exc)
            continue

    return {"kind": "default"}
