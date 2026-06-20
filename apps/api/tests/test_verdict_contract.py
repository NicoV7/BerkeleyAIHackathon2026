"""B2 — verdict contract parity & backcompat (pure schema/function tests).

The judge legibility work (WS-1) added four *additive* fields to the verdict
contract: ``why``, ``logic``, ``persuasion`` and ``actor_id``. The contract is
otherwise frozen and persisted as JSON, so this suite pins two guarantees:

1. ``app.schemas.JudgeVerdict`` still validates OLD payloads that predate the new
   fields (they default to ``None``) AND accepts NEW payloads carrying them.
2. ``app.routers.debate._to_verdict`` maps the new fields when present and falls
   back to ``None`` when absent — REST parity with the WS ``verdict`` event.

No DB, no Redis, no network — schema construction + one pure mapping function.
"""
from __future__ import annotations

from app.routers.debate import _to_verdict
from app.schemas import JudgeVerdict

# The minimal pre-WS-1 verdict payload (no why/logic/persuasion/actor_id).
OLD_VERDICT = {
    "turn": 1,
    "target": "wild-001",
    "score": 72.0,
    "rationale": "Solid rebuttal.",
    "damage": 14,
}

# The new fields WS-1 surfaces on top of the frozen contract.
NEW_FIELDS = {
    "why": "Reframed the burden of proof in one clean sentence.",
    "logic": 80.0,
    "persuasion": 65.0,
    "actor_id": "party-001",
}


# --- JudgeVerdict schema backcompat -----------------------------------------


def test_schema_accepts_old_payload_without_new_fields():
    """Old persisted JSON must still validate; new fields default to None."""
    v = JudgeVerdict(**OLD_VERDICT)

    assert v.turn == 1
    assert v.target == "wild-001"
    assert v.score == 72.0
    assert v.rationale == "Solid rebuttal."
    assert v.damage == 14

    # Additive fields are absent -> default None (no required-field break).
    assert v.why is None
    assert v.logic is None
    assert v.persuasion is None
    assert v.actor_id is None


def test_schema_accepts_payload_with_new_fields():
    """New payloads carrying the WS-1 fields validate and round-trip."""
    v = JudgeVerdict(**OLD_VERDICT, **NEW_FIELDS)

    assert v.why == NEW_FIELDS["why"]
    assert v.logic == 80.0
    assert v.persuasion == 65.0
    assert v.actor_id == "party-001"


def test_schema_new_fields_are_optional_individually():
    """Each new field is independently optional (partial new payloads work)."""
    v = JudgeVerdict(**OLD_VERDICT, actor_id="party-001")

    assert v.actor_id == "party-001"
    assert v.why is None
    assert v.logic is None
    assert v.persuasion is None


def test_old_payload_survives_json_round_trip():
    """A model built from old JSON re-serializes without injecting required keys."""
    v = JudgeVerdict(**OLD_VERDICT)
    dumped = v.model_dump()

    # All original keys preserved.
    for key, value in OLD_VERDICT.items():
        assert dumped[key] == value

    # New keys present in the dump but null, so consumers see explicit None.
    assert dumped["why"] is None
    assert dumped["logic"] is None
    assert dumped["persuasion"] is None
    assert dumped["actor_id"] is None


# --- _to_verdict REST mapping parity ----------------------------------------


def test_to_verdict_maps_new_fields_when_present():
    """_to_verdict threads the WS-1 fields through to the REST JudgeVerdict."""
    d = {**OLD_VERDICT, **NEW_FIELDS}
    v = _to_verdict(d)

    assert isinstance(v, JudgeVerdict)
    assert v.turn == 1
    assert v.target == "wild-001"
    assert v.score == 72.0
    assert v.rationale == "Solid rebuttal."
    assert v.damage == 14
    assert v.why == NEW_FIELDS["why"]
    assert v.logic == 80.0
    assert v.persuasion == 65.0
    assert v.actor_id == "party-001"


def test_to_verdict_defaults_new_fields_to_none_when_absent():
    """Old-shape dicts (no new keys) map cleanly, new fields default to None."""
    v = _to_verdict(dict(OLD_VERDICT))

    assert isinstance(v, JudgeVerdict)
    assert v.score == 72.0
    assert v.damage == 14
    assert v.why is None
    assert v.logic is None
    assert v.persuasion is None
    assert v.actor_id is None


def test_to_verdict_partial_new_fields():
    """Mapping is per-key: a dict with only actor_id leaves the rest None."""
    d = {**OLD_VERDICT, "actor_id": "enemy-002"}
    v = _to_verdict(d)

    assert v.actor_id == "enemy-002"
    assert v.why is None
    assert v.logic is None
    assert v.persuasion is None
