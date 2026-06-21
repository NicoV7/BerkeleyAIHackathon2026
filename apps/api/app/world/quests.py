"""quests.py — Dynamic quest generation + completion (Wave 4 living layer).

A quest is a structured request from a quest_giver NPC: "do X, get Y." Quest
types are small + finite so the FE can render them and the server can match
``world_events`` against them mechanically without LLM-side parsing.

Workflow:
    1. Player approaches a quest_giver NPC; FE calls POST /quest/offer/{npc_id}.
    2. ``offer_quest()`` picks a quest template based on world state (uncleared
       dungeons, available bosses, available figure trials) and emits a Quest.
    3. The Quest is appended to the run's event log as ``quest_accepted``.
    4. When the player completes the matching event (boss_defeated etc.),
       ``maybe_complete_quests()`` matches and emits ``quest_completed``.

Quests live ENTIRELY in the event log — no separate Redis store — so a single
source of truth drives both NPC dialogue ("hero of Drystone") and quest state.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.world import event_log


@dataclass
class Quest:
    """A generated quest. Serializable for the FE."""
    quest_id: str
    npc_id: str
    objective: str           # "clear_dungeon" | "defeat_boss" | "recruit_figure" | "deliver"
    target: str              # poi_id / boss_id / figure_id / item_id
    reward: str              # human-readable reward blurb
    title: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "quest_id": self.quest_id,
            "npc_id": self.npc_id,
            "objective": self.objective,
            "target": self.target,
            "reward": self.reward,
            "title": self.title,
            "description": self.description,
        }


def _quest_id(npc_id: str, objective: str, target: str) -> str:
    """Stable id — accepting the same quest twice is a no-op."""
    h = hashlib.md5(f"{npc_id}|{objective}|{target}".encode("utf-8")).hexdigest()
    return f"q_{h[:10]}"


async def is_active(run_id: str, quest_id: str) -> bool:
    return (
        await event_log.has(run_id, "quest_accepted", quest_id=quest_id)
        and not await event_log.has(run_id, "quest_completed", quest_id=quest_id)
    )


async def offer_quest(
    run_id: str,
    npc_id: str,
    *,
    candidate_dungeons: list[tuple[str, str]] | None = None,
) -> Quest | None:
    """Pick a quest from world state. Returns None if nothing fits.

    candidate_dungeons: [(poi_key, name)] from the canonical world. The first
    uncleared dungeon becomes the quest target; if all are cleared, no quest is
    offered (the FE shows a "no work today" message).
    """
    candidates = candidate_dungeons or []
    for poi_key, name in candidates:
        if await event_log.has(run_id, "dungeon_cleared", poi=poi_key):
            continue
        objective = "clear_dungeon"
        target = poi_key
        qid = _quest_id(npc_id, objective, target)
        if await is_active(run_id, qid):
            return _build_quest_obj(qid, npc_id, objective, target, name)
        quest = _build_quest_obj(qid, npc_id, objective, target, name)
        await event_log.append(
            run_id,
            "quest_accepted",
            quest_id=qid,
            objective=objective,
            target=target,
            npc_id=npc_id,
        )
        return quest
    return None


def _build_quest_obj(
    qid: str, npc_id: str, objective: str, target: str, target_name: str
) -> Quest:
    if objective == "clear_dungeon":
        return Quest(
            quest_id=qid,
            npc_id=npc_id,
            objective=objective,
            target=target,
            reward="50 gold + a healing draught",
            title=f"Clear {target_name}",
            description=(
                f"Strange voices echo from {target_name}. Make your way to its depths "
                "and silence whatever speaks. Return when it is done."
            ),
        )
    return Quest(
        quest_id=qid,
        npc_id=npc_id,
        objective=objective,
        target=target,
        reward="?",
        title="Quest",
        description="A favor is asked.",
    )


async def maybe_complete_quests(run_id: str, event_kind: str, **event_data: Any) -> list[str]:
    """Scan accepted-but-not-completed quests; mark any that match this event.

    Returns the list of newly-completed ``quest_id`` strings. Callers can show
    a popup / give rewards based on the return value.
    """
    accepted = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    completed_now: list[str] = []
    for evt in accepted:
        if evt.kind != "quest_accepted":
            continue
        qid = evt.data.get("quest_id")
        if not qid:
            continue
        if await event_log.has(run_id, "quest_completed", quest_id=qid):
            continue
        # Match table: which world event satisfies which quest objective.
        objective = evt.data.get("objective")
        target = evt.data.get("target")
        matched = False
        if objective == "clear_dungeon" and event_kind == "dungeon_cleared":
            matched = event_data.get("poi") == target
        elif objective == "defeat_boss" and event_kind == "boss_defeated":
            matched = event_data.get("boss_id") == target
        elif objective == "recruit_figure" and event_kind == "figure_recruited":
            matched = event_data.get("figure_id") == target
        if matched:
            await event_log.append(run_id, "quest_completed", quest_id=qid)
            completed_now.append(qid)
    return completed_now


async def list_quests(run_id: str) -> list[dict[str, Any]]:
    """All quests on the run with their accept/complete status."""
    events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    by_id: dict[str, dict[str, Any]] = {}
    for evt in events:
        if evt.kind == "quest_accepted":
            qid = evt.data.get("quest_id")
            if not qid:
                continue
            by_id[qid] = {
                **evt.data,
                "status": "accepted",
                "accepted_at": evt.ts,
            }
        elif evt.kind == "quest_completed":
            qid = evt.data.get("quest_id")
            if qid in by_id:
                by_id[qid]["status"] = "completed"
                by_id[qid]["completed_at"] = evt.ts
    return list(by_id.values())
