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
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
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

try:  # skill_engine (sibling agent): skill_instructions(skill_name, attacker_type) -> str
    from app.debate.skill_engine import skill_instructions  # type: ignore
except Exception:  # noqa: BLE001 — never fail if the module isn't present yet
    def skill_instructions(name: Any, atype: Any) -> str:  # type: ignore[misc]
        return ""


CAPTURABLE_HP_FRACTION = 0.25
TRANSCRIPT_WINDOW = 16  # last N utterances injected into actor context


# ---- Latency fast-path ------------------------------------------------------


def _actor_model(actor: "Combatant") -> str:
    """Model id for an actor's turn.

    Prefer the combatant's own pinned model (e.g. a trained genome's), else the
    fast actor model from config. This keeps debater turns on a SMALL, fast model
    so a round completes in seconds instead of timing out at the old 120s ceiling.
    """
    return actor.model or settings.actor_model


def _actor_timeout() -> float:
    """Per-call budget for a single NON-streaming actor `complete` (seconds).

    Uses the larger `llm_call_timeout_s` (~28s) so a real argument has room to
    finish. The live STREAMING path does NOT use this — it is guarded by the
    small `first_token_timeout_s` (see `_first_token_timeout`).
    """
    return float(getattr(settings, "llm_call_timeout_s", 28) or 28)


def _actor_max_tokens() -> int:
    """Token cap for an actor turn — small for punchy 1-2 sentence arguments."""
    return int(getattr(settings, "actor_max_tokens", 64) or 64)


# Real, side-taking fallback arguments. When the model fails/stalls we still want
# the transcript to READ like a debate, not the old "(NAME presses the point on
# TOPIC.)" filler AND not vague meta-hedging ("I take the side that survives the
# hardest question"). Each template makes a CONCRETE claim about the actual topic
# and explicitly argues FOR or AGAINST it. Keyed by (side, type) so the player's
# FOR monster and the enemy's AGAINST monster never read identically, and varied
# within a bucket by a turn seed.
# Every template explicitly contains the capitalized stance word ("FOR" / "AGAINST")
# so the stance is unmistakable in the transcript.
_FALLBACK_FOR: dict[str, list[str]] = {
    "LOGOS": [
        "I argue FOR {topic}: the evidence runs in its favor and every objection raised "
        "against it collapses once you ask for specifics. The case stands.",
        "I am FOR {topic} — trace the consequences and the benefits are concrete while the "
        "harms opponents warn of stay hypothetical. The logic backs my side.",
    ],
    "PATHOS": [
        "I stand FOR {topic} because of who it actually helps — real people gain, and "
        "denying them that to protect a tidy abstraction is the real cost here.",
        "Picture the people {topic} lifts up. That human stake is exactly why I argue FOR "
        "it, and why the opposing view rings hollow.",
    ],
    "ETHOS": [
        "I argue FOR {topic}: the people who do this work and live with it back it, while "
        "the case against leans on claims it cannot stand behind.",
        "I am FOR {topic} on principle, not convenience — it holds to its own standards, "
        "which is more than the opposition's position can honestly say.",
    ],
}
_FALLBACK_AGAINST: dict[str, list[str]] = {
    "LOGOS": [
        "I argue AGAINST {topic}: the strongest version of the claim still fails on its "
        "own terms, and the supposed evidence dissolves under scrutiny.",
        "I am AGAINST {topic} — follow the consequences and the costs are concrete while "
        "the promised upside is speculative. The reasoning cuts against it.",
    ],
    "PATHOS": [
        "I am AGAINST {topic} because of who actually pays for it — real people bear the "
        "downside, and that human cost is the whole reason I oppose it.",
        "Think about who gets hurt if {topic} wins. That stake is exactly why I stand "
        "AGAINST it, not for some tidy slogan.",
    ],
    "ETHOS": [
        "I argue AGAINST {topic}: the people closest to it know its standards don't hold, "
        "and the case for it leans on assurances it cannot back up.",
        "I am AGAINST {topic} on principle — it cannot meet the very test it sets for "
        "itself, and consistency demands rejecting it.",
    ],
}
_FALLBACK_FOR_DEFAULT = [
    "I argue FOR {topic}: it carries the stronger reasons and the case against it falls "
    "apart the moment you press it for specifics.",
]
_FALLBACK_AGAINST_DEFAULT = [
    "I argue AGAINST {topic}: the case for it carries hidden costs and collapses under a "
    "single concrete question.",
]


async def prewarm_models(models: list[str] | None = None) -> None:
    """Fire tiny throwaway completions so the first real turn isn't cold.

    Low-risk and best-effort: bounded by the short per-call timeout, every error
    is swallowed, and it does nothing when ``settings.prewarm_enabled`` is off.
    Called on encounter creation so the model is loaded before the battle starts.
    """
    if not getattr(settings, "prewarm_enabled", False):
        return
    targets = models or [settings.actor_model, settings.judge_model_fast]
    seen: set[str] = set()
    for m in targets:
        if not m or m in seen:
            continue
        seen.add(m)
        try:
            await gateway.complete(
                [{"role": "user", "content": "ok"}],
                model=m,
                max_tokens=1,
                timeout=_actor_timeout(),
            )
        except Exception:  # noqa: BLE001 — prewarm is purely best-effort
            pass


def _side_for(actor: "Combatant") -> str:
    """The debate side an actor argues: 'for' or 'against'.

    Prefers an explicit `side` set on the combatant (deterministic assignment in
    the round runners); otherwise derives a sensible default from role so the
    party argues FOR the topic and enemies argue AGAINST it.
    """
    s = (getattr(actor, "side", None) or "").lower()
    if s in ("for", "against"):
        return s
    return "for" if actor.role == "party" else "against"


def _fallback_argument(actor: "Combatant", topic: str, turn_seed: int = 0) -> str:
    """A REAL short argument (1-2 sentences, takes a concrete side) for model failure.

    Explicitly argues FOR or AGAINST the actual topic — never the old "presses the
    point" stub and never vague meta-hedging. Keyed by (side, type) so the FOR and
    AGAINST monsters read differently, varied by a turn seed within a bucket. This
    is the graceful-degradation text the live/headless/human paths fall back to.
    """
    topic_str = topic or "this question"
    side = _side_for(actor)
    type_key = (actor.type or "").upper()
    if side == "against":
        pool = _FALLBACK_AGAINST.get(type_key, _FALLBACK_AGAINST_DEFAULT)
    else:
        pool = _FALLBACK_FOR.get(type_key, _FALLBACK_FOR_DEFAULT)
    # Deterministic index — Python's salted hash() varies per process (test flake);
    # use a stable digest so the same (monster, turn) always picks the same line.
    key = f"{actor.monster_id}:{turn_seed}".encode()
    idx = int.from_bytes(hashlib.md5(key).digest()[:4], "big") % len(pool)
    return pool[idx].format(topic=topic_str)


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
    # Which side of the debate this combatant argues — "for" | "against". Assigned
    # deterministically by the round runners (party lead = FOR the topic, enemy
    # lead = AGAINST it) so stances are crystal clear in prompts + utterances.
    side: Optional[str] = None

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
    team = "your team" if actor.role == "party" else "the opposing team"
    side = _side_for(actor)
    stance_verb = "FOR" if side == "for" else "AGAINST"
    persona = _persona_line(actor)
    skills = _skill_names(actor)

    sys_parts = [
        f"You are {actor.name}, a debate combatant of type {actor.type}, on {team}.",
        f"The debate topic is: {topic}",
        # UNMISTAKABLE stance anchor: name the side, name the topic, and forbid
        # conceding or flipping. Playtest bug: an AGAINST enemy argued FOR — this
        # block (used by BOTH the streaming and non-streaming builders) makes the
        # assigned side impossible to misread.
        f"YOUR ASSIGNED SIDE: {stance_verb}. You argue {stance_verb} the topic "
        f'"{topic}". Argue ONLY for the {stance_verb} side. Make a concrete claim '
        f"about {topic} and state plainly why you are {stance_verb} it. Do NOT "
        f"concede, do NOT switch sides, and do NOT argue the other side — even to "
        f"steelman it. Every sentence must support the {stance_verb} position.",
        "Make ONE sharp, persuasive argument that advances your side and rebuts the "
        "latest opposing point. Be vivid and concise (1-2 sentences). Speak in-character. "
        "Do NOT narrate, use stage directions, or hedge with vague meta-talk about "
        "debate strategy — argue the actual topic with a concrete claim.",
    ]
    if persona:
        sys_parts.append(f"Your persona — {persona}.")
    if skills:
        sys_parts.append(f"Your debate skills: {', '.join(skills)}.")
    if action.get("behavior"):
        sys_parts.append(f"Your commander orders you to: {action['behavior']}.")
    if action.get("skill"):
        sys_parts.append(f"Use your skill: {action['skill']}.")
        # Enrich with the skill_engine seam (defensive — never fails if absent).
        try:
            extra = skill_instructions(action.get("skill"), actor.type)
        except Exception:  # noqa: BLE001
            extra = ""
        if extra:
            sys_parts.append(str(extra))
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
            messages,
            model=_actor_model(actor),
            temperature=0.8,
            max_tokens=_actor_max_tokens(),
            timeout=_actor_timeout(),
        )
        text = (text or "").strip()
    except Exception:  # noqa: BLE001 — stalled/failed model: fall back to real text
        text = ""
    if not text:
        text = _fallback_argument(actor, topic, turn_seed=len(transcript))
    return _sanitize(text)


def _sanitize(text: str) -> str:
    """Strip control characters that break strict JSON parsers (incl. the
    browser's JSON.parse) — small local models occasionally emit them."""
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t")


# First-token wall-clock guard for the live streaming path. On a contended CPU
# the dominant risk is a stalled model that never emits a first token; we'd rather
# fail this one utterance (and fall back to a REAL templated argument) than hang
# the whole WS round. This uses the SMALL `first_token_timeout_s` (~8s) — NOT the
# larger non-streaming `llm_call_timeout_s` — so "first token <= 6-8s" holds and a
# slow-to-start model falls back fast. Tokens after the first stream freely.
def _first_token_timeout() -> float:
    return float(getattr(settings, "first_token_timeout_s", 8) or 8)


STREAM_FIRST_TOKEN_TIMEOUT_S = _first_token_timeout()


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

    A first-token timeout (`_first_token_timeout()`, the small
    `settings.first_token_timeout_s`) guards a stalled CPU model: if no token
    arrives in time the utterance fails over to the fallback
    line instead of hanging the round. `_generate_utterance` is intentionally
    left untouched so the headless/REST paths keep their determinism.
    """
    messages = _build_actor_messages(actor, topic, transcript, action, memories, name_lookup)
    parts: list[str] = []
    try:
        agen = gateway.stream(
            messages, model=_actor_model(actor), temperature=0.8,
            max_tokens=_actor_max_tokens(),
        )
        first = True
        while True:
            try:
                if first:
                    chunk = await asyncio.wait_for(
                        agen.__anext__(), timeout=_first_token_timeout()
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
        full = _fallback_argument(actor, topic, turn_seed=len(transcript))
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

    # CLEAR SIDES (deterministic): party argues FOR the topic, enemies AGAINST it.
    # Threaded onto the combatants so prompts + emitted utterances carry the stance.
    for c in combatants:
        c.side = "for" if c.role == "party" else "against"

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
                        "side": actor.side,
                        "text": chunk["text"],
                    },
                )
            else:  # "done"
                text = chunk["text"]

        utt = {
            "turn": turn_no,
            "actor_id": actor.monster_id,
            "actor_role": actor.role,
            "side": actor.side,
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
        # Emit ONE HpUpdate per combatant ({monster_id, hp, max_hp}) — the frontend
        # patches combatants by monster_id, so a {id: hp} map silently no-ops (HP bug).
        yield Event("hp", {"monster_id": c.monster_id, "hp": c.hp, "max_hp": c.max_hp})

    phase, capturable = _phase_for(combatants)
    yield Event("phase", {"phase": phase, "capturable_ids": capturable, "turn_no": turn_no})


async def get_transcript_safe(eid: str) -> list[dict[str, Any]]:
    from app.redis_state import get_transcript
    try:
        return await get_transcript(eid)
    except Exception:  # noqa: BLE001
        return []


# ---- Human-argues path (WS-G) -----------------------------------------------


def _lead(combatants: list[Combatant], role: str) -> Optional[Combatant]:
    """Highest-initiative alive combatant on a side (level desc, then name)."""
    alive = [c for c in combatants if c.role == role and c.alive]
    if not alive:
        return None
    return sorted(alive, key=lambda c: (-c.level, c.name))[0]


def _resolve_skill(actor: Combatant, skill_id: str | None) -> tuple[Optional[str], str, float]:
    """Resolve a skill_id (== skill name) against an actor's skills.

    Returns (skill_name, attack_type, power). Falls back to the actor's own type
    and power 1.0 when the skill is absent or unnamed.
    """
    if not skill_id:
        return None, actor.type, 1.0
    for s in actor.skills or []:
        if isinstance(s, dict) and str(s.get("name", "")) == skill_id:
            return (
                str(s.get("name")),
                str(s.get("type") or actor.type),
                float(s.get("power", 1.0) or 1.0),
            )
        if isinstance(s, str) and s == skill_id:
            return s, actor.type, 1.0
    # Unknown skill name — still record it as used, but no type/power bonus.
    return skill_id, actor.type, 1.0


async def run_human_round_stream(
    eid: str,
    topic: str,
    combatants: list[Combatant],
    run_id: str | None,
    start_turn: int,
    momentum: dict[str, float],
    player_text: str,
    skill_id: str | None = None,
):
    """Async generator running ONE human-driven round. Yields Event objects.

    The player's typed argument is the lead party monster's turn; the lead enemy
    rebuts autonomously. RESPONSIVENESS + CLARITY fixes:

      * STREAMED enemy rebuttal — the enemy uses `_stream_utterance` (same token
        mechanism as the auto round), so the WS emits `token` events as they arrive
        and perceived latency is first-token, not full generation.
      * PARALLELIZED judging — the player's already-known argument is judged in a
        task that runs CONCURRENTLY with enemy generation; the enemy text is judged
        right after and both are `asyncio.gather`-ed. LLM calls overlap instead of
        running strictly sequentially, while damage still applies once both scores
        exist.
      * CLEAR SIDES — the player's lead argues FOR the topic, the enemy lead argues
        AGAINST it (deterministic). `side` is threaded into both prompts and carried
        on the emitted utterance/token events.

    Damage applies (the player's chosen skill scales it), then hp/phase emit.
    Mirrors `run_round_stream`'s event protocol so the WS/REST callers are
    unchanged. Mutates `combatants` HP and `momentum` in place.
    """
    from app.redis_state import (
        ENCOUNTER_TTL_SECONDS,
        append_utterance,
        get_redis,
        k_judge,
        set_hp,
    )

    name_lookup = {c.monster_id: c.name for c in combatants}
    turn_no = start_turn

    player = _lead(combatants, "party")
    enemy = _lead(combatants, "enemy")
    if player is None or enemy is None:
        phase, capturable = _phase_for(combatants)
        yield Event("phase", {"phase": phase, "capturable_ids": capturable, "turn_no": turn_no})
        return

    # --- CLEAR SIDES (deterministic): the player's lead argues FOR the topic, the
    # lead enemy argues AGAINST it. Thread it onto the combatants so the prompt +
    # the emitted utterances both carry the stance. ---
    player.side = "for"
    enemy.side = "against"

    skill_name, attack_type, skill_power = _resolve_skill(player, skill_id)
    fallback_model = next((c.model for c in combatants if c.model), None)

    # --- Player turn (human-typed) ---
    turn_no += 1
    text = _sanitize((player_text or "").strip()) or f"({player.name} stays silent on {topic}.)"
    player_utt = {
        "turn": turn_no,
        "actor_id": player.monster_id,
        "actor_role": "party",
        "side": player.side,
        "skill_used": skill_name,
        "text": text,
        "ts": time.time(),
    }
    await append_utterance(eid, player_utt)
    yield Event("utterance", player_utt)

    enemy_turn = turn_no + 1

    # --- PARALLELIZE: judge the player's ALREADY-KNOWN argument concurrently with
    # generating (streaming) the enemy rebuttal. The player score doesn't depend on
    # the enemy text, so the two LLM calls overlap instead of running back-to-back.
    # We kick off the player-judge task first, then stream the enemy so its tokens
    # reach the WS as they arrive (perceived latency = first enemy token).
    player_judge_task = asyncio.create_task(
        score_round(
            topic,
            [{"actor_id": player.monster_id, "text": text}],
            fallback_model=fallback_model,
        )
    )

    # --- Enemy rebuttal (autonomous, STREAMED) ---
    battle_state = _build_battle_state(enemy, combatants, topic, enemy_turn, 50.0, momentum)
    action = _decide_action(enemy, battle_state)
    memories = await _gather_memories(enemy, topic, run_id)
    transcript = await get_transcript_safe(eid)

    enemy_text = ""
    try:
        async for chunk in _stream_utterance(
            enemy, topic, transcript, action, memories, name_lookup
        ):
            if chunk["kind"] == "token":
                yield Event(
                    "token",
                    {
                        "turn": enemy_turn,
                        "actor_id": enemy.monster_id,
                        "side": enemy.side,
                        "text": chunk["text"],
                    },
                )
            else:  # "done"
                enemy_text = chunk["text"]
    except Exception:  # noqa: BLE001 — never let a stream error orphan the judge task
        if not enemy_text:
            enemy_text = _fallback_argument(enemy, topic, turn_seed=len(transcript))

    turn_no = enemy_turn
    enemy_utt = {
        "turn": turn_no,
        "actor_id": enemy.monster_id,
        "actor_role": "enemy",
        "side": enemy.side,
        "skill_used": action.get("skill"),
        "text": enemy_text,
        "ts": time.time(),
    }
    await append_utterance(eid, enemy_utt)
    yield Event("utterance", enemy_utt)

    # --- Judge: the player score was computed concurrently with enemy generation;
    # now judge the enemy text. Gather both so any straggler resolves together. ---
    enemy_judge_task = asyncio.create_task(
        score_round(
            topic,
            [{"actor_id": enemy.monster_id, "text": enemy_text}],
            fallback_model=fallback_model,
        )
    )
    player_scores, enemy_scores = await asyncio.gather(
        player_judge_task, enemy_judge_task
    )
    score_by_id = {js.actor_id: js for js in (*player_scores, *enemy_scores)}

    # --- Apply damage (player's skill scales their hit; enemy uses 1.0) ---
    verdicts: list[dict[str, Any]] = []
    side_net = {"party": 0.0, "enemy": 0.0}
    for actor, atk_type, mult in (
        (player, attack_type, skill_power),
        (enemy, enemy.type, 1.0),
    ):
        js = score_by_id.get(actor.monster_id)
        if js is None:
            continue
        side_net[actor.role] += js.score - 50.0
        target = _lead(combatants, "enemy" if actor.role == "party" else "party")
        if target is None:
            continue
        mom = momentum.get(actor.role, 1.0)
        dmg = compute_damage(
            score=js.score,
            attacker_type=atk_type,
            defender_type=target.type,
            skill_mult=mult,
            momentum=mom,
            attacker_level=actor.level,
            defender_level=target.level,
        )
        target.hp = max(0, target.hp - dmg)
        verdicts.append(
            {
                "turn": turn_no,
                "actor_id": actor.monster_id,
                "target": target.monster_id,
                "score": js.score,
                "rationale": js.rationale,
                "damage": dmg,
            }
        )

    # Momentum update (same shape as _apply_round_damage).
    for side in ("party", "enemy"):
        swing = side_net[side] / 100.0
        momentum[side] = max(0.7, min(1.3, momentum.get(side, 1.0) + swing * 0.15))

    # --- Persist + emit verdicts, hp, phase ---
    r = get_redis()
    for v in verdicts:
        payload = {
            "turn": v["turn"],
            "target": v["target"],
            "score": v["score"],
            "rationale": v["rationale"],
            "damage": v["damage"],
        }
        await r.rpush(k_judge(eid), _json_dumps(payload))
        yield Event("verdict", {**payload, "actor_id": v["actor_id"]})
    await r.expire(k_judge(eid), ENCOUNTER_TTL_SECONDS)

    for c in combatants:
        await set_hp(eid, c.monster_id, c.hp)
        # Emit ONE HpUpdate per combatant ({monster_id, hp, max_hp}) — the frontend
        # patches combatants by monster_id, so a {id: hp} map silently no-ops (HP bug).
        yield Event("hp", {"monster_id": c.monster_id, "hp": c.hp, "max_hp": c.max_hp})

    phase, capturable = _phase_for(combatants)
    yield Event("phase", {"phase": phase, "capturable_ids": capturable, "turn_no": turn_no})


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)


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
