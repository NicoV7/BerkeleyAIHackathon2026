"""Debate orchestrator — the turn-based battle engine (WS-B core).

Two paths share one engine:

  * Live path (routers): state lives in Redis (frozen key schema in
    app.redis_state). `run_round_stream` is an async generator of typed events
    (utterance / verdict / hp / phase) so the WS endpoint can stream and the
    /turn + /auto endpoints can drain it.

  * Headless path (WS-F training): `run_self_play(...)` runs an in-memory debate
    with no Redis / no WS and returns a transcript + net score dict.

Round structure (one "round" == one pass over the turn queue):
  1. For each actor in initiative order, build context (shared transcript +
     topic + side + RAG memories + persona/skills). Party actors consult the
     gambits seam for a forced action. Generate the utterance; append to the
     shared transcript immediately so later actors in the round see it.
  2. After the round, the judge scores every utterance at once. Each scored
     utterance damages the opposing side (split across living enemies). Momentum
     updates from the round's net swing.
  3. Win/loss when a whole side hits 0 HP. Wild enemies under 25% HP are
     flagged capturable.

Integration seams are imported defensively (try/except ImportError) so WS-B can
run before WS-C / WS-D / WS-A land.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.debate.damage import compute_damage
from app.debate.judge import score_round
from app.gateway.gateway import gateway

# ---- Defensive integration seams --------------------------------------------

try:  # WS-C: gambits. choose_action(monster_dict, battle_state) -> action dict
    from app.debate.gambits import choose_action as _choose_action  # type: ignore
except Exception:  # noqa: BLE001
    _choose_action = None

try:  # WS-D: RAG. retrieve(monster_id, query, run_id=..., k=...) -> list[str]
    from app.memory.retriever import retrieve as _retrieve  # type: ignore
except Exception:  # noqa: BLE001
    _retrieve = None


CAPTURABLE_HP_FRACTION = 0.25
TRANSCRIPT_WINDOW = 16  # last N utterances injected into actor context


# ---- In-memory combatant model (used by both paths) -------------------------


@dataclass
class Combatant:
    monster_id: str
    name: str
    type: str  # DebateType value, e.g. "LOGOS"
    role: str  # "party" | "enemy"
    hp: int
    max_hp: int
    level: int = 1
    owner: str = "wild"  # "player" | "wild" | "enemy"
    persona: dict[str, Any] = field(default_factory=dict)
    harness: dict[str, Any] = field(default_factory=dict)
    skills: list[Any] = field(default_factory=list)
    model: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self.hp > 0


# ---- Event type (engine -> caller) ------------------------------------------


@dataclass
class Event:
    kind: str  # "utterance" | "verdict" | "hp" | "phase"
    data: dict[str, Any]


# ---- Context building -------------------------------------------------------


async def _gather_memories(actor: Combatant, topic: str, run_id: str | None) -> list[str]:
    if _retrieve is None:
        return []
    try:
        # WS-D's retrieve(session, monster_id, query, k=, event_type=) needs a
        # session; open a short-lived one here so RAG actually injects.
        from app.db.session import SessionLocal

        async with SessionLocal() as session:
            res = _retrieve(session, actor.monster_id, topic, k=3)
            if asyncio.iscoroutine(res):
                res = await res
        out: list[str] = []
        for m in res or []:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict):
                out.append(str(m.get("summary") or m.get("content") or ""))
            else:
                out.append(str(getattr(m, "summary", m)))
        return [s for s in out if s]
    except Exception:  # noqa: BLE001
        return []


def _decide_action(actor: Combatant, battle_state: dict[str, Any]) -> dict[str, Any]:
    """Consult the gambits seam (party only). Falls back to a default action."""
    default = {"behavior": "argue your strongest point", "skill": None, "target": None, "tone": None}
    if actor.role != "party" or _choose_action is None:
        return default
    try:
        monster_dict = {
            "id": actor.monster_id,
            "name": actor.name,
            "type": actor.type,
            "level": actor.level,
            "skills": actor.skills,
            "persona": actor.persona,
            "harness": actor.harness,
        }
        action = _choose_action(monster_dict, battle_state)
        if isinstance(action, dict) and action:
            return {**default, **action}
    except Exception:  # noqa: BLE001
        pass
    return default


def _build_battle_state(
    actor: Combatant,
    combatants: list[Combatant],
    topic: str,
    turn_no: int,
    last_verdict_score: float,
    momentum: dict[str, float],
) -> dict[str, Any]:
    """The dict passed to WS-C's choose_action. Keys are a stable contract."""
    return {
        "hp": {c.monster_id: c.hp for c in combatants},
        "max_hp": {c.monster_id: c.max_hp for c in combatants},
        "last_verdict_score": last_verdict_score,
        "turn_no": turn_no,
        "topic": topic,
        "momentum": dict(momentum),
        "self_id": actor.monster_id,
        "ally_ids": [c.monster_id for c in combatants if c.role == actor.role and c.monster_id != actor.monster_id],
        "enemy_ids": [c.monster_id for c in combatants if c.role != actor.role],
    }


def _persona_line(actor: Combatant) -> str:
    p = actor.persona or {}
    bits = []
    if p.get("style"):
        bits.append(f"style: {p['style']}")
    if p.get("voice"):
        bits.append(f"voice: {p['voice']}")
    if p.get("bio"):
        bits.append(str(p["bio"]))
    return "; ".join(bits)


def _skill_names(actor: Combatant) -> list[str]:
    names = []
    for s in actor.skills or []:
        if isinstance(s, str):
            names.append(s)
        elif isinstance(s, dict) and s.get("name"):
            names.append(str(s["name"]))
    return names


def _build_actor_messages(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
) -> list[dict[str, str]]:
    side = "your team" if actor.role == "party" else "the opposing team"
    persona = _persona_line(actor)
    skills = _skill_names(actor)

    sys_parts = [
        f"You are {actor.name}, a debate combatant of type {actor.type}. "
        f"You are debating on {side}.",
        f"The debate topic is: {topic}",
        "Make ONE sharp, persuasive argument that advances your side and rebuts "
        "the latest opposing point. Be vivid and concise (2-4 sentences). Speak "
        "in-character; do not narrate or use stage directions.",
    ]
    if persona:
        sys_parts.append(f"Your persona — {persona}.")
    if skills:
        sys_parts.append(f"Your debate skills: {', '.join(skills)}.")
    if action.get("behavior"):
        sys_parts.append(f"Your commander orders you to: {action['behavior']}.")
    if action.get("skill"):
        sys_parts.append(f"Use your skill: {action['skill']}.")
    if action.get("tone"):
        sys_parts.append(f"Adopt a {action['tone']} tone.")
    if memories:
        sys_parts.append("What you remember: " + " | ".join(memories))

    # Recent shared transcript window.
    window = transcript[-TRANSCRIPT_WINDOW:]
    if window:
        convo = []
        for u in window:
            who = name_lookup.get(u.get("actor_id", ""), u.get("actor_id", "?"))
            convo.append(f"{who}: {u.get('text','')}")
        history = "Recent exchange:\n" + "\n".join(convo)
    else:
        history = "You speak first. Open strong."

    user = history + "\n\nNow give your argument."
    return [
        {"role": "system", "content": " ".join(sys_parts)},
        {"role": "user", "content": user},
    ]


async def _generate_utterance(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
) -> str:
    messages = _build_actor_messages(actor, topic, transcript, action, memories, name_lookup)
    try:
        text = await gateway.complete(
            messages, model=actor.model, temperature=0.8, max_tokens=180
        )
        text = (text or "").strip()
    except Exception as e:  # noqa: BLE001
        text = ""
    if not text:
        text = f"({actor.name} presses the point on {topic}.)"
    return _sanitize(text)


def _sanitize(text: str) -> str:
    """Strip control characters that break strict JSON parsers (incl. the
    browser's JSON.parse) — small local models occasionally emit them."""
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t")


# First-token wall-clock guard for the live streaming path. On a contended CPU
# gemma3 the dominant demo risk is a stalled model that never emits a first
# token; we'd rather fail this one utterance (and fall back to a templated line)
# than hang the whole WS round. Tuned for cold local models.
STREAM_FIRST_TOKEN_TIMEOUT_S = 18.0


async def _stream_utterance(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
):
    """Live streaming twin of `_generate_utterance`.

    Async generator that REUSES `_build_actor_messages` for the prompt, iterates
    `gateway.stream(...)`, sanitizes every token via `_sanitize`, and accumulates
    the full text. Each yield is a dict:

      * `{"kind": "token", "text": <sanitized chunk>}` per streamed chunk, and
      * exactly one terminating `{"kind": "done", "text": <full accumulated text>}`
        carrying the canonical assembled utterance (with the same templated
        fallback as `_generate_utterance` if the stream produced nothing).

    A first-token timeout (`STREAM_FIRST_TOKEN_TIMEOUT_S`) guards a stalled CPU
    model: if no token arrives in time the utterance fails over to the fallback
    line instead of hanging the round. `_generate_utterance` is intentionally
    left untouched so the headless/REST paths keep their determinism.
    """
    messages = _build_actor_messages(actor, topic, transcript, action, memories, name_lookup)
    parts: list[str] = []
    try:
        agen = gateway.stream(
            messages, model=actor.model, temperature=0.8, max_tokens=180
        )
        first = True
        while True:
            try:
                if first:
                    chunk = await asyncio.wait_for(
                        agen.__anext__(), timeout=STREAM_FIRST_TOKEN_TIMEOUT_S
                    )
                    first = False
                else:
                    chunk = await agen.__anext__()
            except StopAsyncIteration:
                break
            piece = _sanitize(chunk or "")
            if not piece:
                continue
            parts.append(piece)
            yield {"kind": "token", "text": piece}
        # Best-effort close of the underlying generator.
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        # Stalled or errored stream: drop whatever partial text we have so the
        # fallback below produces a clean, complete line.
        parts = []

    full = "".join(parts).strip()
    if not full:
        full = f"({actor.name} presses the point on {topic}.)"
    full = _sanitize(full)
    yield {"kind": "done", "text": full}


# ---- Core round logic (shared) ----------------------------------------------


def _apply_round_damage(
    combatants: list[Combatant],
    scored: list[tuple],
    momentum: dict[str, float],
) -> list[dict[str, Any]]:
    """Apply damage from a round's scored utterances. Returns verdict dicts.

    Each scored utterance damages the opposing side, split across its living
    members. Momentum nudges based on net per-side scoring this round.

    Each `scored` entry is `(actor, score, rationale)` and may carry an optional
    4th element: a dict of extra verdict fields (e.g. why/logic/persuasion) that
    is merged into the emitted verdict. Older 3-tuple callers stay supported.
    """
    verdicts: list[dict[str, Any]] = []
    side_net: dict[str, float] = {"party": 0.0, "enemy": 0.0}

    for entry in scored:
        actor, score, rationale = entry[0], entry[1], entry[2]
        extra: dict[str, Any] = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
        side_net[actor.role] += (score - 50.0)
        enemy_role = "enemy" if actor.role == "party" else "party"
        targets = [c for c in combatants if c.role == enemy_role and c.alive]
        if not targets:
            continue
        mom = momentum.get(actor.role, 1.0)
        total_dmg = 0
        # Split damage across living defenders.
        for target in targets:
            dmg = compute_damage(
                score=score,
                attacker_type=actor.type,
                defender_type=target.type,
                skill_mult=1.0,
                momentum=mom,
                attacker_level=actor.level,
                defender_level=target.level,
            )
            dmg = max(0, round(dmg / len(targets)))
            target.hp = max(0, target.hp - dmg)
            total_dmg += dmg
        verdicts.append(
            {
                "actor_id": actor.monster_id,
                "target": targets[0].monster_id,
                "score": score,
                "rationale": rationale,
                "damage": total_dmg,
                **extra,
            }
        )

    # Momentum update: winners of the round gain, losers lose. Clamp 0.7..1.3.
    for side in ("party", "enemy"):
        swing = side_net[side] / 100.0  # ~ -0.5..0.5
        momentum[side] = max(0.7, min(1.3, momentum.get(side, 1.0) + swing * 0.15))

    return verdicts


def _phase_for(combatants: list[Combatant]) -> tuple[str, list[str]]:
    """Compute phase + capturable wild ids from current HP."""
    party_alive = any(c.alive for c in combatants if c.role == "party")
    enemy_alive = any(c.alive for c in combatants if c.role == "enemy")
    if not enemy_alive:
        return "won", []
    if not party_alive:
        return "lost", []
    capturable = [
        c.monster_id
        for c in combatants
        if c.role == "enemy" and c.owner == "wild" and c.alive
        and c.hp <= c.max_hp * CAPTURABLE_HP_FRACTION
    ]
    if capturable:
        return "capturable", capturable
    return "debating", []


# ---- Live (Redis) path ------------------------------------------------------


def _pick_active_party(party: list[Combatant], momentum: dict[str, float]) -> Combatant:
    """Autonomous initiative: pick which party agent argues this round.

    Heuristic — the healthiest living party agent leads (deterministic tiebreak by
    name). This is the "agents decide who goes first" behavior used in Auto mode.
    """
    return sorted(
        party,
        key=lambda c: (-(c.hp / c.max_hp if c.max_hp else 0.0), c.name),
    )[0]


async def run_round_stream(
    eid: str,
    topic: str,
    combatants: list[Combatant],
    run_id: str | None,
    start_turn: int,
    momentum: dict[str, float],
    last_verdict_score: float = 50.0,
    active_party_id: str | None = None,
):
    """Async generator running ONE round. Yields Event objects.

    Dungeon-RPG turn model: exactly ONE party agent argues per round (the
    player-picked `active_party_id`, or auto-picked via `_pick_active_party` when
    None), followed by every living enemy. Mutates `combatants` HP and `momentum`
    in place; the caller persists state to Redis as events arrive.
    """
    from app.redis_state import append_utterance, set_hp

    name_lookup = {c.monster_id: c.name for c in combatants}
    turn_no = start_turn

    living = [c for c in combatants if c.alive]
    party = [c for c in living if c.role == "party"]
    enemies = [c for c in living if c.role == "enemy"]
    if party:
        active = next((c for c in party if c.monster_id == active_party_id), None)
        if active is None:
            active = _pick_active_party(party, momentum)
        order = [active] + enemies
    else:
        order = living
    scored_inputs: list[dict[str, Any]] = []
    actor_by_id = {c.monster_id: c for c in combatants}

    # --- Speaking pass (sequential so each actor sees prior utterances) ---
    for actor in order:
        if not actor.alive:
            continue
        turn_no += 1
        battle_state = _build_battle_state(
            actor, combatants, topic, turn_no, last_verdict_score, momentum
        )
        action = _decide_action(actor, battle_state)
        memories = await _gather_memories(actor, topic, run_id)
        transcript = await get_transcript_safe(eid)

        # Live token streaming: emit additive `token` events as the model thinks,
        # then the canonical `utterance` event with the full assembled text. The
        # `done` chunk carries the fallback-resolved text, so a stalled/empty
        # stream still produces a complete utterance (whole-utterance fallback).
        text = ""
        async for chunk in _stream_utterance(
            actor, topic, transcript, action, memories, name_lookup
        ):
            if chunk["kind"] == "token":
                yield Event(
                    "token",
                    {
                        "turn": turn_no,
                        "actor_id": actor.monster_id,
                        "text": chunk["text"],
                    },
                )
            else:  # "done"
                text = chunk["text"]

        utt = {
            "turn": turn_no,
            "actor_id": actor.monster_id,
            "actor_role": actor.role,
            "skill_used": action.get("skill"),
            "text": text,
            "ts": time.time(),
        }
        await append_utterance(eid, utt)
        scored_inputs.append({"actor_id": actor.monster_id, "text": text})
        yield Event("utterance", utt)

    # --- Judge the whole round at once ---
    fallback_model = next((c.model for c in combatants if c.model), None)
    scores = await score_round(topic, scored_inputs, fallback_model=fallback_model)
    scored: list[tuple] = []
    for js in scores:
        actor = actor_by_id.get(js.actor_id)
        if actor:
            extra = {
                "why": js.why,
                "logic": js.logic,
                "persuasion": js.persuasion,
            }
            scored.append((actor, js.score, js.rationale, extra))

    verdicts = _apply_round_damage(combatants, scored, momentum)

    # Persist + emit verdicts and HP.
    from app.redis_state import get_redis, k_judge, ENCOUNTER_TTL_SECONDS
    import json as _json

    r = get_redis()
    for v in verdicts:
        verdict_payload = {
            "turn": turn_no,
            "target": v["target"],
            "score": v["score"],
            "rationale": v["rationale"],
            "damage": v["damage"],
            "actor_id": v["actor_id"],
            "why": v.get("why"),
            "logic": v.get("logic"),
            "persuasion": v.get("persuasion"),
        }
        await r.rpush(k_judge(eid), _json.dumps(verdict_payload))
        yield Event("verdict", verdict_payload)
    await r.expire(k_judge(eid), ENCOUNTER_TTL_SECONDS)

    for c in combatants:
        await set_hp(eid, c.monster_id, c.hp)
    yield Event("hp", {c.monster_id: c.hp for c in combatants})

    phase, capturable = _phase_for(combatants)
    yield Event("phase", {"phase": phase, "capturable_ids": capturable, "turn_no": turn_no})


async def get_transcript_safe(eid: str) -> list[dict[str, Any]]:
    from app.redis_state import get_transcript
    try:
        return await get_transcript(eid)
    except Exception:  # noqa: BLE001
        return []


# ---- Headless self-play path (WS-F) -----------------------------------------


def _monster_to_combatant(m: Any, role: str) -> Combatant:
    """Accept a Monster ORM row OR a plain dict and build a Combatant."""
    def g(key: str, default: Any = None) -> Any:
        if isinstance(m, dict):
            return m.get(key, default)
        return getattr(m, key, default)

    mtype = g("type", "LOGOS")
    # DebateType enum -> value
    mtype = getattr(mtype, "value", mtype)
    owner = g("owner", "wild")
    owner = getattr(owner, "value", owner)
    max_hp = int(g("max_hp", 100) or 100)
    return Combatant(
        monster_id=str(g("id", f"{role}-{int(time.time()*1000)}")),
        name=str(g("name", role.title())),
        type=str(mtype),
        role=role,
        hp=max_hp,
        max_hp=max_hp,
        level=int(g("level", 1) or 1),
        owner=str(owner),
        persona=dict(g("persona", {}) or {}),
        harness=dict(g("harness", {}) or {}),
        skills=list(g("skills", []) or []),
        model=g("model"),
    )


async def _run_self_play_async(
    party_monster: Any,
    sparring_monster: Any,
    topic: str,
    rounds: int,
) -> dict[str, Any]:
    party = _monster_to_combatant(party_monster, "party")
    enemy = _monster_to_combatant(sparring_monster, "enemy")
    combatants = [party, enemy]
    name_lookup = {c.monster_id: c.name for c in combatants}
    momentum = {"party": 1.0, "enemy": 1.0}

    transcript: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    turn_no = 0
    last_score = 50.0

    for _ in range(max(1, rounds)):
        order = [c for c in combatants if c.alive]
        scored_inputs: list[dict[str, Any]] = []
        actor_by_id = {c.monster_id: c for c in combatants}

        for actor in order:
            if not actor.alive:
                continue
            turn_no += 1
            battle_state = _build_battle_state(
                actor, combatants, topic, turn_no, last_score, momentum
            )
            action = _decide_action(actor, battle_state)
            memories: list[str] = []  # headless: skip RAG for determinism/speed
            text = await _generate_utterance(
                actor, topic, transcript, action, memories, name_lookup
            )
            utt = {
                "turn": turn_no,
                "actor_id": actor.monster_id,
                "actor_role": actor.role,
                "skill_used": action.get("skill"),
                "text": text,
                "ts": time.time(),
            }
            transcript.append(utt)
            scored_inputs.append({"actor_id": actor.monster_id, "text": text})

        fb = next((c.model for c in combatants if c.model), None)
        scores = await score_round(topic, scored_inputs, fallback_model=fb)
        scored: list[tuple[Combatant, float, str]] = []
        for js in scores:
            a = actor_by_id.get(js.actor_id)
            if a:
                scored.append((a, js.score, js.rationale))
                if a.role == "party":
                    last_score = js.score
        round_verdicts = _apply_round_damage(combatants, scored, momentum)
        for v in round_verdicts:
            verdicts.append({**v, "turn": turn_no})

        phase, _cap = _phase_for(combatants)
        if phase in ("won", "lost"):
            break

    # Net score: party total minus enemy total over all verdicts.
    party_ids = {party.monster_id}
    party_total = sum(v["score"] for v in verdicts if v["actor_id"] in party_ids)
    enemy_total = sum(v["score"] for v in verdicts if v["actor_id"] not in party_ids)
    party_count = sum(1 for v in verdicts if v["actor_id"] in party_ids) or 1
    enemy_count = sum(1 for v in verdicts if v["actor_id"] not in party_ids) or 1
    net_score = (party_total / party_count) - (enemy_total / enemy_count)

    final_phase, _ = _phase_for(combatants)
    return {
        "topic": topic,
        "transcript": transcript,
        "verdicts": verdicts,
        "party_id": party.monster_id,
        "sparring_id": enemy.monster_id,
        "party_hp": party.hp,
        "sparring_hp": enemy.hp,
        "party_avg_score": party_total / party_count,
        "sparring_avg_score": enemy_total / enemy_count,
        "net_score": round(net_score, 2),
        "result": final_phase,
        "rounds_played": turn_no // max(1, len(combatants)) if combatants else 0,
    }


def run_self_play(
    party_monster: Any,
    sparring_monster: Any,
    topic: str,
    rounds: int = 3,
) -> dict[str, Any]:
    """Headless self-play debate (WS-F training seam).

    Runs entirely in memory — no Redis, no WebSocket. `party_monster` and
    `sparring_monster` may be Monster ORM rows or plain dicts (id/name/type/
    level/max_hp/persona/harness/skills/model). Returns a dict with
    transcript, verdicts, net_score, and result.

    Sync wrapper around the async engine so non-async callers (training jobs)
    can call it directly; if already inside an event loop, use
    `await _run_self_play_async(...)` instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _run_self_play_async(party_monster, sparring_monster, topic, rounds)
        )
    # Inside a running loop: run in a fresh loop on a worker thread to stay sync.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            asyncio.run,
            _run_self_play_async(party_monster, sparring_monster, topic, rounds),
        )
        return fut.result()
