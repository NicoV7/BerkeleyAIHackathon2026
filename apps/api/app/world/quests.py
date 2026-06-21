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

Quest objectives (WS-2 backend) and the world event that completes them:
    clear_dungeon   <- dungeon_cleared {poi}
    defeat_boss     <- boss_defeated {boss_id}
    recruit_figure  <- figure_recruited {figure_id}
    hunt_enemy      <- enemy_killed {enemy_kind} (or {monster_id})
    find_item       <- item_found {item_key}
    debate_npc      <- npc_debated {npc_id}

Each quest carries a ``reward`` blurb AND a structured ``reward_spec``
(``{"coins": int, "items": [item_key, ...]}``) that the world router pays out
through the Wave-0 economy award helpers when ``maybe_complete_quests`` fires.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.world import event_log


@dataclass
class Quest:
    """A generated quest. Serializable for the FE."""
    quest_id: str
    npc_id: str
    objective: str           # see "Quest objectives" in the module docstring
    target: str              # poi_id / boss_id / figure_id / enemy_kind / item_key / npc_id
    reward: str              # human-readable reward blurb
    title: str
    description: str
    # Structured reward the world router pays out via the Wave-0 economy helpers
    # the moment the quest completes: {"coins": int, "items": [item_key, ...]}.
    reward_spec: dict[str, Any] = field(default_factory=dict)
    # Optional minimap-pin coordinate {"x": int, "y": int} for the quest target.
    target_xy: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quest_id": self.quest_id,
            "npc_id": self.npc_id,
            "objective": self.objective,
            "target": self.target,
            "reward": self.reward,
            "title": self.title,
            "description": self.description,
            "reward_spec": self.reward_spec,
            "target_xy": self.target_xy,
        }


# Reward templates per objective: structured coins + item grants paid out by the
# world router on completion. Kept here so quest design lives next to quest logic.
_REWARD_SPECS: dict[str, dict[str, Any]] = {
    "clear_dungeon": {"coins": 50, "items": ["potion_hp_small"]},
    "defeat_boss": {"coins": 120, "items": ["potion_hp_small", "potion_mp_small"]},
    "recruit_figure": {"coins": 80, "items": []},
    "hunt_enemy": {"coins": 40, "items": ["potion_hp_small"]},
    "find_item": {"coins": 30, "items": []},
    "debate_npc": {"coins": 60, "items": ["potion_mp_small"]},
}


def _reward_blurb(spec: dict[str, Any]) -> str:
    """Human-readable reward string derived from a reward_spec."""
    parts: list[str] = []
    coins = int(spec.get("coins") or 0)
    if coins:
        parts.append(f"{coins} gold")
    for item_key in spec.get("items") or []:
        parts.append(str(item_key).replace("_", " "))
    return " + ".join(parts) if parts else "a reward"


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
        quest = _build_quest_obj(
            _quest_id(npc_id, "clear_dungeon", poi_key),
            npc_id,
            "clear_dungeon",
            poi_key,
            name,
        )
        return await _accept(run_id, quest)
    return None


async def offer_typed_quest(
    run_id: str,
    npc_id: str,
    objective: str,
    target: str,
    *,
    target_name: str | None = None,
    target_xy: dict[str, int] | None = None,
) -> Quest:
    """Offer ANY quest objective (the WS-2 generic entry point).

    Idempotent: re-offering the same (npc, objective, target) returns the already
    accepted quest without appending a second ``quest_accepted`` event. Used by
    the onboarding first-quest grant and by NPCs that hand out hunt/find/debate
    quests (not just dungeon clears).
    """
    qid = _quest_id(npc_id, objective, target)
    quest = _build_quest_obj(
        qid, npc_id, objective, target, target_name or target, target_xy=target_xy
    )
    return await _accept(run_id, quest)


async def _accept(run_id: str, quest: Quest) -> Quest:
    """Append a ``quest_accepted`` event for ``quest`` unless it is already active.

    Returns the quest either way so accepting twice is a safe no-op.
    """
    if await is_active(run_id, quest.quest_id):
        return quest
    await event_log.append(
        run_id,
        "quest_accepted",
        quest_id=quest.quest_id,
        objective=quest.objective,
        target=quest.target,
        npc_id=quest.npc_id,
        title=quest.title,
        description=quest.description,
        reward=quest.reward,
        reward_spec=quest.reward_spec,
        target_xy=quest.target_xy,
    )
    return quest


# ---------------------------------------------------------------------------
# Quest object construction (one arm per objective)
# ---------------------------------------------------------------------------


def _build_quest_obj(
    qid: str,
    npc_id: str,
    objective: str,
    target: str,
    target_name: str,
    *,
    target_xy: dict[str, int] | None = None,
) -> Quest:
    spec = dict(_REWARD_SPECS.get(objective, {"coins": 25, "items": []}))
    reward = _reward_blurb(spec)

    titles_descs: dict[str, tuple[str, str]] = {
        "clear_dungeon": (
            f"Clear {target_name}",
            f"Strange voices echo from {target_name}. Make your way to its depths "
            "and silence whatever speaks. Return when it is done.",
        ),
        "defeat_boss": (
            f"Defeat {target_name}",
            f"{target_name} has cowed every debater in the region. Best them in a "
            "battle of wits and break their hold.",
        ),
        "recruit_figure": (
            f"Recruit {target_name}",
            f"Seek out {target_name}, win their trial debate, and earn the right to "
            "summon them in battle.",
        ),
        "hunt_enemy": (
            f"Hunt the {target_name}",
            f"A {target_name} has been harrying travelers. Track it down and defeat "
            "it in debate.",
        ),
        "find_item": (
            f"Recover the {target_name}",
            f"The {target_name} was lost somewhere in the wilds. Find it and bring it "
            "back.",
        ),
        "debate_npc": (
            f"Debate {target_name}",
            f"{target_name} doubts your rhetoric. Win a debate against them to prove "
            "your mettle.",
        ),
    }
    title, description = titles_descs.get(objective, ("Quest", "A favor is asked."))
    return Quest(
        quest_id=qid,
        npc_id=npc_id,
        objective=objective,
        target=target,
        reward=reward,
        title=title,
        description=description,
        reward_spec=spec,
        # POI pin coords for the minimap, if the caller resolved any. Parsed back
        # from a "kind:x:y" target when absent (see _target_xy_from).
        target_xy=target_xy or _target_xy_from(target),
    )


def _target_xy_from(target: str) -> dict[str, int] | None:
    """Best-effort POI pin coords from a ``kind:x:y`` target (e.g. den:112:160)."""
    parts = target.split(":")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return {"x": int(parts[1]), "y": int(parts[2])}
    return None


# Match table: (objective, event_kind) -> event_data key compared to the target.
# A ``hunt_enemy`` target may be matched by EITHER the enemy_kind or a concrete
# monster_id, so it is handled as a special case below.
_OBJECTIVE_MATCH: dict[tuple[str, str], str] = {
    ("clear_dungeon", "dungeon_cleared"): "poi",
    ("defeat_boss", "boss_defeated"): "boss_id",
    ("recruit_figure", "figure_recruited"): "figure_id",
    ("find_item", "item_found"): "item_key",
    ("debate_npc", "npc_debated"): "npc_id",
}


def _matches(objective: str, target: str, event_kind: str, event_data: dict[str, Any]) -> bool:
    """True if a world event satisfies a quest objective for ``target``."""
    if objective == "hunt_enemy" and event_kind == "enemy_killed":
        # A hunt is satisfied by killing the named enemy kind OR a specific id.
        return target in (
            str(event_data.get("enemy_kind") or ""),
            str(event_data.get("monster_id") or ""),
        )
    key = _OBJECTIVE_MATCH.get((objective, event_kind))
    return key is not None and event_data.get(key) == target


async def maybe_complete_quests(run_id: str, event_kind: str, **event_data: Any) -> list[str]:
    """Scan accepted-but-not-completed quests; mark any that match this event.

    Returns the list of newly-completed ``quest_id`` strings. Each completion
    appends a ``quest_completed`` event carrying the quest's ``reward_spec`` so
    the world router can pay rewards out via the Wave-0 economy helpers. Callers
    can read the specs back with ``completed_reward_specs``.
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
        objective = evt.data.get("objective") or ""
        target = evt.data.get("target") or ""
        if _matches(objective, target, event_kind, event_data):
            await event_log.append(
                run_id,
                "quest_completed",
                quest_id=qid,
                reward_spec=evt.data.get("reward_spec") or {},
            )
            completed_now.append(qid)
    return completed_now


async def completed_reward_specs(
    run_id: str, quest_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Map quest_id -> reward_spec for the given completed quests.

    Reads the ``quest_completed`` events (which carry the reward_spec snapshot) so
    the world router can pay rewards out exactly once per completion.
    """
    if not quest_ids:
        return {}
    wanted = set(quest_ids)
    out: dict[str, dict[str, Any]] = {}
    for evt in await event_log.recent(run_id, limit=event_log.MAX_EVENTS):
        if evt.kind != "quest_completed":
            continue
        qid = evt.data.get("quest_id")
        if qid in wanted and qid not in out:
            out[qid] = dict(evt.data.get("reward_spec") or {})
    return out


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
