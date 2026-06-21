"""event_log.py — Append-only per-run world event log (Wave 4 living layer).

The log is the SOURCE OF TRUTH for NPC reactivity. When the player clears a
dungeon, defeats a boss, or recruits a figure, that event is appended here.
NPCs read the recent tail when generating dialogue, so a townsfolk can say
"the hero of Drystone returns" the moment the player walks back to town.

Storage:
    Redis list ``runevt:{run_id}`` — each entry is a JSON object
        {"kind": <str>, "data": <dict>, "ts": <float-unix-seconds>}
    Truncated to MAX_EVENTS so a long playthrough doesn't unboundedly grow.

Public surface:
    await append(run_id, kind, **data)
    await recent(run_id, limit=20) -> list[Event]
    await has(run_id, kind, **filters) -> bool
    await count(run_id, kind) -> int

Tests can patch ``get_redis`` to swap in fakeredis; the module never imports
fakeredis itself so production stays clean.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.redis_state import get_redis

MAX_EVENTS = 256

EventKind = str  # "dungeon_cleared" | "boss_defeated" | "figure_recruited" | "region_entered" | "quest_accepted" | "quest_completed"


@dataclass
class Event:
    kind: EventKind
    data: dict[str, Any]
    ts: float

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "data": self.data, "ts": self.ts})

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        d = json.loads(raw)
        return cls(kind=d.get("kind", ""), data=d.get("data") or {}, ts=float(d.get("ts", 0.0)))


def _key(run_id: str) -> str:
    return f"runevt:{run_id}"


async def append(run_id: str, kind: EventKind, **data: Any) -> Event:
    """Append a new event. Truncates the head if the log exceeds MAX_EVENTS."""
    evt = Event(kind=kind, data=data, ts=time.time())
    r = get_redis()
    key = _key(run_id)
    await r.rpush(key, evt.to_json())
    # Trim to MAX_EVENTS (LTRIM is cheap; bounded list).
    await r.ltrim(key, -MAX_EVENTS, -1)
    return evt


async def recent(run_id: str, limit: int = 20) -> list[Event]:
    """Return the last ``limit`` events (most-recent last)."""
    r = get_redis()
    raws = await r.lrange(_key(run_id), -limit, -1)
    out: list[Event] = []
    for raw in raws:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            out.append(Event.from_json(raw))
        except Exception:  # noqa: BLE001 — never crash on a bad log entry
            continue
    return out


async def has(run_id: str, kind: EventKind, **filters: Any) -> bool:
    """True if any event of ``kind`` matches ALL ``filters`` (data k/v pairs)."""
    events = await recent(run_id, limit=MAX_EVENTS)
    for evt in events:
        if evt.kind != kind:
            continue
        if all(evt.data.get(k) == v for k, v in filters.items()):
            return True
    return False


async def count(run_id: str, kind: EventKind) -> int:
    """Count events of a given kind across the (truncated) log."""
    events = await recent(run_id, limit=MAX_EVENTS)
    return sum(1 for e in events if e.kind == kind)


async def all_events(run_id: str) -> list[Event]:
    """Return every event in the log (capped at MAX_EVENTS by storage)."""
    return await recent(run_id, limit=MAX_EVENTS)


async def clear(run_id: str) -> None:
    """Test/maintenance hook: drop the run's event log."""
    r = get_redis()
    await r.delete(_key(run_id))
