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
  2. After the round, the judge scores every utterance at once. The side with the
     stronger average score wins the cycle, and only that side's best utterance
     deals damage. Momentum updates from the round's net swing.
  3. Win/loss when a whole side hits 0 HP. Wild enemies under 25% HP are
     flagged capturable.

Integration seams are imported defensively (try/except ImportError) so WS-B can
run before WS-C / WS-D / WS-A land.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.debate.damage import compute_damage
from app.debate.judge import heuristic_score, score_round
from app.debate.latency_metrics import RoundTimer
from app.debate.topics import domain_match_mult
from app.gateway.gateway import gateway
from app.party.persona import (
    ensure_battle_sentence_floor,
    ensure_battle_reactions,
    harness_prompt_line,
    normalize_harness,
    normalize_persona,
    persona_prompt_line,
    sanitize_battle_utterance,
    select_battle_reaction,
)

# ---- Defensive integration seams --------------------------------------------

try:  # WS-C: gambits. choose_action(monster_dict, battle_state) -> action dict
    from app.debate.gambits import choose_action as _choose_action  # type: ignore
except Exception:  # noqa: BLE001
    _choose_action = None

try:  # WS-D: RAG. retrieve(monster_id, query, run_id=..., k=...) -> list[str]
    from app.memory.retriever import retrieve as _retrieve  # type: ignore
except Exception:  # noqa: BLE001
    _retrieve = None

try:  # skill_engine (sibling agent): skill prompt + metadata helpers
    from app.debate.skill_engine import skill_instructions, skill_metadata  # type: ignore
except Exception:  # noqa: BLE001 — never fail if the module isn't present yet
    def skill_instructions(name: Any, atype: Any) -> str:  # type: ignore[misc]
        return ""

    def skill_metadata(name: Any) -> dict[str, Any]:  # type: ignore[misc]
        return {}


CAPTURABLE_HP_FRACTION = 0.25
TRANSCRIPT_WINDOW = 16  # last N utterances injected into actor context

# WS-5: the slug of the MP counter-skill. When this skill is active AND paid for,
# the caster's prompt is primed with the enemy's predicted next line (read from
# the already-present transcript / cached opening) so the caster pre-empts it —
# with ZERO extra model calls. Compared via the same slug rule skill_engine uses.
COUNTER_SKILL_SLUG = "rhetorical_flourish"


def _is_counter_skill(skill_name: str | None) -> bool:
    """True when ``skill_name`` resolves to the Rhetorical Flourish counter-skill.

    Slug-normalizes so "Rhetorical Flourish" / "rhetorical_flourish" both match,
    mirroring skill_engine.slugify without importing it on the hot path.
    """
    if not skill_name:
        return False
    try:
        from app.debate.skill_engine import slugify

        return slugify(skill_name) == COUNTER_SKILL_SLUG
    except Exception:  # noqa: BLE001 — never fail the round on a slug hiccup
        import re

        norm = re.sub(r"[^0-9a-zA-Z]+", "_", str(skill_name).strip().lower()).strip("_")
        return norm == COUNTER_SKILL_SLUG


def _last_enemy_line(transcript: list[dict[str, Any]], caster: "Combatant") -> str:
    """Most recent utterance by the OPPOSING side, read from the shared transcript.

    The transcript is ALREADY fetched once per round by the runners, so this is a
    pure in-memory scan — no Redis round-trip, no model call. Returns "" when no
    opposing line exists yet (round 1), in which case the caller falls back to the
    materialized opening as the predicted line.
    """
    if not transcript:
        return ""
    caster_role = caster.role
    for u in reversed(transcript):
        role = u.get("actor_role")
        text = (u.get("text") or "").strip()
        if not text:
            continue
        # The "enemy" of a party caster is role=="enemy" and vice-versa. Skip the
        # caster's own side; the first opposing line walking backwards is the one
        # they most need to pre-empt.
        if role and role != caster_role:
            return text
    return ""


def _counter_skill_instruction(predicted_line: str) -> str:
    """Build the pre-empt-and-rebut instruction injected for the counter-skill.

    ``predicted_line`` is the opponent's already-existing last line (or, on round
    1, their materialized opening). This text is appended to the caster's SYSTEM
    prompt — it does NOT trigger any generation by itself; the caster's single,
    normal turn is simply primed with the opponent's predicted move.
    """
    line = (predicted_line or "").strip()
    if not line:
        return (
            "RHETORICAL FLOURISH (counter): your opponent has not spoken yet. "
            "Pre-empt their most likely opening objection to your side, dismantle "
            "it in advance, and land a memorable one-line flourish."
        )
    # Keep the injected quote bounded so the counter-skill never grows context
    # unbounded (a single enemy turn is already short, but clamp defensively).
    if len(line) > 600:
        line = line[:600].rstrip() + "…"
    return (
        "RHETORICAL FLOURISH (counter): your opponent's most likely next move is "
        f'this line: "{line}". Pre-empt it — name the rebuttal they will reach '
        "for, dismantle it BEFORE they can make it, and compress your win into one "
        "rhythmic, memorable sentence that lands last."
    )


async def _resolve_counter_context(
    caster: "Combatant",
    skill_name: str | None,
    transcript: list[dict[str, Any]],
    topic: str,
    combatants: list["Combatant"] | None = None,
) -> str | None:
    """Build the WS-5 counter-skill prompt prime, or ``None`` when not active.

    ZERO extra model calls by construction:
      * If ``skill_name`` is NOT the counter-skill -> ``None`` (normal turn).
      * Predicted line = the most recent OPPOSING utterance already in
        ``transcript`` (pure in-memory scan).
      * Round 1 (no opposing line yet): use the enemy's already-materialized
        opening via ``get_cached_opening`` — a pure Redis RETRIEVAL that returns
        ``None`` on a miss and NEVER generates (we deliberately avoid
        ``get_or_create_opening`` so the counter-skill can't trigger a
        completion). On a miss we still return a generic pre-empt instruction
        (no line, still no call).

    The MP gate runs BEFORE this in the round runners, so reaching here means the
    skill was paid for.
    """
    if not _is_counter_skill(skill_name):
        return None
    predicted = _last_enemy_line(transcript, caster)
    if not predicted:
        # Round 1 with no opposing line yet — predict the enemy's opening. Pure
        # cache retrieval (no generation): a miss simply yields a line-less prime.
        try:
            from app.debate.materialize import get_cached_opening

            cached = await get_cached_opening(topic)
            if cached:
                predicted = cached.strip()
        except Exception:  # noqa: BLE001 — cache lookup is best-effort, never a call
            predicted = ""
    return _counter_skill_instruction(predicted)

# Gacha Wave B: end-of-round MP regen. +10 / round, clamped to max_mp. Lives as
# a module-level constant so tests + helpers in app.debate.mp share one source.
MP_REGEN_PER_ROUND = 10

# Winner-only cycle damage uses judge margin, not raw score. A 70-60 cycle maps to
# an effective damage score of 80 (50 + 10 margin * 2 + 10 winner bonus), which
# keeps ordinary winning cycles meaningful while close/tied cycles stay controlled.
CYCLE_DAMAGE_MARGIN_MULT = 2.0
CYCLE_DAMAGE_WIN_BONUS = 10.0
CYCLE_TIE_EPSILON = 0.5


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


def _battle_damage_multiplier() -> float:
    """Applied-damage pacing multiplier for shortening encounters."""
    return max(0.1, float(getattr(settings, "battle_damage_multiplier", 1.0) or 1.0))


def _human_judge_deadline() -> float:
    """A4 reconciliation deadline (seconds) for the human-round judge call.

    Autoplan finding (A4): the optimistic `estimate` shows the heuristic score
    instantly, but HP still waits on `await score_round(...)`, which on a stalled
    single-slot Ollama serially times out each candidate in judge.py (~28-56s) —
    leaving the round visually dangling after the estimate. We cap the LLM judge
    at this deadline; on timeout we COMMIT the already-displayed heuristic score
    as authoritative (compute damage from it, emit the normal verdict+hp) so the
    round always settles within the budget. Configurable via
    `human_judge_deadline_s` (default 10s)."""
    return float(getattr(settings, "human_judge_deadline_s", 10) or 10)


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
    "CHAOS": [
        "I argue FOR {topic}: the opposition is defending the wrong frame, and once that "
        "frame breaks, their neat objection has nowhere left to stand.",
        "I am FOR {topic} because the supposed downside is a decoy. The real risk is "
        "letting a bad premise decide the whole debate.",
    ],
    "SOCRATIC": [
        "I argue FOR {topic}: if the opposing side is right, they should be able to name "
        "the exact principle it violates. They cannot.",
        "I am FOR {topic}; answer one question first: what concrete harm outweighs the "
        "benefit, and where is your proof?",
    ],
    "RHETORIC": [
        "I argue FOR {topic}: the other side offers a locked door and calls it caution; "
        "my side offers a key and calls it progress.",
        "I am FOR {topic} because the case against it is all smoke and no fire. Strip "
        "away the posture, and the better line is ours.",
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
    "CHAOS": [
        "I argue AGAINST {topic}: the case for it only works inside a rigged frame, and "
        "that frame breaks the second we ask who benefits.",
        "I am AGAINST {topic} because its promise is misdirection. Change the question "
        "to consequences, and the shiny answer turns brittle.",
    ],
    "SOCRATIC": [
        "I argue AGAINST {topic}: if it is so sound, why does its defense dodge the first "
        "hard exception? That silence is the argument.",
        "I am AGAINST {topic}; before accepting it, answer what happens when its core "
        "assumption fails in the real world.",
    ],
    "RHETORIC": [
        "I argue AGAINST {topic}: the slogan sounds bright, but bright slogans can still "
        "cast long shadows. This one does.",
        "I am AGAINST {topic} because the promise is polished and the cost is buried. "
        "Good rhetoric should reveal, not conceal.",
    ],
}
_FALLBACK_FOR_DEFAULT = [
    "I argue FOR {topic}: it carries the stronger reasons and the case against it falls "
    "apart the moment you press it for specifics.",
    "I stand FOR {topic}: when the claims are tested, my side keeps its footing and the "
    "opposition is left defending abstractions.",
]
_FALLBACK_AGAINST_DEFAULT = [
    "I argue AGAINST {topic}: the case for it carries hidden costs and collapses under a "
    "single concrete question.",
    "I stand AGAINST {topic}: its promise is too vague, its risks are too concrete, and "
    "the burden of proof has not been met.",
]


# WS-4 warm-path state. A model is "warm" once a prewarm ping at encounter create
# has loaded it into Ollama (so it's resident, not paying the cold-load tax). The
# live streaming path widens its first-token budget ONLY for warm models — a
# truly cold/stalled model is still bounded by the small cold guard, so we lower
# the fallback rate on healthy models without re-introducing the cold hang.
_WARM_MODELS: set[str] = set()


def _mark_warm(model: str | None) -> None:
    if model:
        _WARM_MODELS.add(model)


def is_model_warm(model: str | None) -> bool:
    """True once ``model`` has been prewarmed this process (WS-4)."""
    return bool(model) and model in _WARM_MODELS


def _reset_warm_state() -> None:
    """Test/dev helper — forget all warm models so the cold budget applies."""
    _WARM_MODELS.clear()


async def _ollama_keep_alive(model: str) -> bool:
    """Send a tiny keep-alive ping to Ollama with the ``keep_alive`` option set so
    the actor model stays RESIDENT across the battle's idle gaps (turn-to-turn
    thinking, the player typing). Best-effort and isolated from the off-limits
    gateway: posts directly to ``/api/chat`` with ``stream=False`` and a 1-token
    cap. Returns True on a clean load. Never raises — any failure (Ollama absent,
    httpx missing, non-200) is swallowed by the caller.

    This is the WS-4 stand-in for "keep_alive on the actor model": the gateway's
    payload is owned by another fleet and doesn't forward keep_alive, so we issue
    the keep_alive ping ourselves at encounter create rather than touching it.
    """
    keep = str(getattr(settings, "ollama_keep_alive", "") or "").strip()
    if not keep:
        return False
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "stream": False,
        "keep_alive": keep,
        "options": {"num_predict": 1},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(_actor_timeout(), connect=5.0)) as client:
        r = await client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
        r.raise_for_status()
    return True


async def prewarm_models(
    models: list[str] | None = None,
    topic: str | None = None,
    enemy_model: str | None = None,
) -> None:
    """Fire tiny throwaway completions so the first real turn isn't cold, and
    (A1) materialize the enemy OPENING for ``topic`` during the encounter-load
    idle window.

    Low-risk and best-effort: bounded by the short per-call timeout, every error
    is swallowed, and prewarm-completions do nothing when ``settings.prewarm_enabled``
    is off. Called on encounter creation so the model is loaded before the battle
    starts. Pass ``topic`` (and optionally the lead enemy's ``enemy_model``) to also
    pre-generate + cache the opening on the idle window so the first enemy turn is a
    pure cache retrieval — a no-op on a cache hit. The opening pre-gen runs even when
    prewarm-completions are disabled (it IS the warm-up that matters for Track A) but
    only when a topic is supplied, so existing arg-less callers are unchanged.

    WS-4: each model that prewarms cleanly is recorded WARM (``is_model_warm``) so
    the live streaming path can widen its first-token budget for it, and the actor
    model gets a ``keep_alive`` ping so it stays resident across the battle's idle
    gaps. Both are best-effort — a failed warm-up simply leaves the model "cold"
    and the small first-token guard keeps applying.
    """
    # A1 — opening materialization. Runs on the encounter-load idle window so the
    # first enemy turn (run_human_round_stream) retrieves instead of generating.
    # A successful pregen IS a warm-up of the opening model, so mark it warm.
    if topic:
        try:
            from app.debate.materialize import (
                _opening_model,
                pregenerate_both_openings,
            )

            # WS-4 #10: warm BOTH the AGAINST (enemy) and FOR (player lead) openings
            # for this topic, one at a time, so the first turn on either side is a
            # pure retrieval. A successful pregen also warms the opening model.
            await pregenerate_both_openings(topic, enemy_model)
            _mark_warm(_opening_model(enemy_model))
        except Exception:  # noqa: BLE001 — best-effort, never block encounter create
            pass

    if not getattr(settings, "prewarm_enabled", False):
        return
    targets = models or [settings.actor_model, settings.judge_model_fast]
    seen: set[str] = set()
    for m in targets:
        if not m or m in seen:
            continue
        seen.add(m)
        # Prefer the keep_alive ping for the actor/judge models so they stay
        # resident; fall back to the gateway throwaway if keep_alive is disabled
        # or the direct ping fails. Either success marks the model warm.
        warmed = False
        try:
            warmed = await _ollama_keep_alive(m)
        except Exception:  # noqa: BLE001 — keep_alive ping is best-effort
            warmed = False
        if not warmed:
            try:
                await gateway.complete(
                    [{"role": "user", "content": "ok"}],
                    model=m,
                    max_tokens=1,
                    timeout=_actor_timeout(),
                )
                warmed = True
            except Exception:  # noqa: BLE001 — prewarm is purely best-effort
                warmed = False
        if warmed:
            _mark_warm(m)


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
    # ---- Gacha-wave stats (additive; defaults mirror Monster model defaults so
    # pre-gacha rows / dicts keep the original numerical behavior). ----
    atk: int = 10
    # `def` is a Python keyword — the dataclass field stays `def_` to match
    # the Monster ORM attribute (the column itself is just "def" in SQL).
    def_: int = 10
    max_mp: int = 50
    domain: str = "GENERAL"
    # True for the player's chosen-avatar monster — the permanent "main character"
    # the lead-selection prefers over level/HP. Always False for enemies.
    is_avatar: bool = False

    @property
    def alive(self) -> bool:
        return self.hp > 0


# ---- Event type (engine -> caller) ------------------------------------------


@dataclass
class Event:
    kind: str  # "utterance" | "verdict" | "hp" | "phase"
    data: dict[str, Any]


def _event_timing(started_at: float | None = None) -> dict[str, Any]:
    """Add optional server timing metadata to live events."""
    out: dict[str, Any] = {"server_ts": time.time()}
    if started_at is not None:
        out["elapsed_ms"] = round((time.monotonic() - started_at) * 1000)
    return out


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


def _actor_skill_blob(actor: Combatant, skill_id: str | None) -> dict[str, Any]:
    """Return the actor-owned skill dict matching ``skill_id``, if present."""
    if not skill_id:
        return {}
    for s in actor.skills or []:
        if isinstance(s, dict) and str(s.get("name", "")) == skill_id:
            return dict(s)
        if isinstance(s, str) and s == skill_id:
            return {"name": s}
    return {}


def _resolved_skill_spec(actor: Combatant, skill_id: str | None) -> dict[str, Any]:
    """Merge global skill metadata with the actor's stored skill blob."""
    if not skill_id:
        return {}
    base = skill_metadata(skill_id)
    owned = _actor_skill_blob(actor, skill_id)
    spec = {**base, **owned}
    if not spec:
        return {}
    spec.setdefault("name", skill_id)
    spec.setdefault("type", actor.type)
    spec.setdefault("power", 1.0)
    spec.setdefault("effect_kind", "agent_argument")
    spec.setdefault("target", "enemy")
    spec.setdefault("duration_turns", 0)
    spec.setdefault("requires_prompt", False)
    spec.setdefault("modifiers", {})
    return spec


def _skill_modifiers(spec: dict[str, Any]) -> dict[str, Any]:
    mods = spec.get("modifiers")
    return dict(mods) if isinstance(mods, dict) else {}


def _modifier_float(spec: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(_skill_modifiers(spec).get(key, default))
    except (TypeError, ValueError):
        return default


def _modifier_int(spec: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(float(_skill_modifiers(spec).get(key, default)))
    except (TypeError, ValueError):
        return default


def _effect_payload(
    *,
    skill: dict[str, Any],
    source: Combatant,
    target: Combatant | None,
    message: str,
    turn_no: int,
) -> dict[str, Any]:
    """Build the visible WS/Redis status payload for a skill effect."""
    return {
        "skill_id": skill.get("name"),
        "skill_name": skill.get("name"),
        "effect_kind": skill.get("effect_kind", "agent_argument"),
        "source_id": source.monster_id,
        "source_name": source.name,
        "target_id": target.monster_id if target else None,
        "target_name": target.name if target else None,
        "duration_turns": int(skill.get("duration_turns", 0) or 0),
        "turn_no": turn_no,
        "message": message,
        "modifiers": _skill_modifiers(skill),
    }


def _persona_line(actor: Combatant) -> str:
    """Render all supported persona flavors into a concise prompt line."""
    return persona_prompt_line(normalize_persona(actor.persona, fallback_name=actor.name))


def _harness_line(actor: Combatant) -> str:
    """Render trained harness fields for prompt injection."""
    return harness_prompt_line(normalize_harness(actor.harness, role=actor.role))


def _skill_names(actor: Combatant) -> list[str]:
    names = []
    for s in actor.skills or []:
        if isinstance(s, str):
            names.append(s)
        elif isinstance(s, dict) and s.get("name"):
            names.append(str(s["name"]))
    return names


def _argument_transcript(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only debate turns, excluding event reaction chatter."""
    return [u for u in transcript if not u.get("reaction_state")]


def _has_opposing_turn(actor: Combatant, transcript: list[dict[str, Any]]) -> bool:
    """Whether this actor has an opposing utterance to answer."""
    for utt in _argument_transcript(transcript):
        role = utt.get("actor_role")
        if role in ("party", "enemy"):
            if role != actor.role:
                return True
            continue
        if utt.get("actor_id") and utt.get("actor_id") != actor.monster_id:
            return True
    return False


def _build_actor_messages(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
    counter_context: str | None = None,
) -> list[dict[str, str]]:
    team = "your team" if actor.role == "party" else "the opposing team"
    side = _side_for(actor)
    stance_verb = "FOR" if side == "for" else "AGAINST"
    persona = _persona_line(actor)
    harness = _harness_line(actor)
    skills = _skill_names(actor)
    has_opposing_turn = _has_opposing_turn(actor, transcript)
    if has_opposing_turn:
        turn_contract = (
            "Make ONE sharp, persuasive argument that advances your side and rebuts the "
            "latest opposing point. Output exactly TWO short plain sentences: first answer "
            "the latest opposing claim, second press one decisive reason for your side."
        )
    else:
        turn_contract = (
            "You are opening for your side. Output exactly TWO short plain sentences: first "
            "make one concrete opening claim, second support it with one decisive reason. "
            "Do not mention that no opponent has spoken."
        )

    sys_parts = [
        f"You are {actor.name}, a debate combatant of type {actor.type}, on {team}.",
        f"The debate topic is: {topic}",
    ]
    if harness:
        sys_parts.append(f"Your trained battle harness — {harness}.")
    sys_parts.extend([
        # UNMISTAKABLE stance anchor: name the side, name the topic, and forbid
        # conceding or flipping. Playtest bug: an AGAINST enemy argued FOR — this
        # block (used by BOTH the streaming and non-streaming builders) makes the
        # assigned side impossible to misread.
        f"YOUR ASSIGNED SIDE: {stance_verb}. You argue {stance_verb} the topic "
        f'"{topic}". Argue ONLY for the {stance_verb} side. Make a concrete claim '
        f"about {topic} and state plainly why you are {stance_verb} it. Do NOT "
        f"concede, do NOT switch sides, and do NOT argue the other side — even to "
        f"steelman it. Every sentence must support the {stance_verb} position.",
        turn_contract + " "
        "Keep each sentence under 22 words. "
        "No headings, markdown, bullets, labels like Claim/Support/Rebuttal, stage "
        "directions, prompt descriptions, assigned-stance descriptions, or vague "
        "meta-talk about debate strategy.",
    ])
    if persona:
        sys_parts.append(f"Your persona — {persona}.")
    if skills:
        sys_parts.append(f"Your debate skills: {', '.join(skills)}.")
    if action.get("behavior"):
        sys_parts.append(f"Your commander orders you to: {action['behavior']}.")
    if action.get("prompt_bonus"):
        sys_parts.append(f"Skill effect on this argument: {action['prompt_bonus']}.")
    if action.get("status_contract"):
        sys_parts.append(f"Temporary battle status: {action['status_contract']}.")
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
    # WS-5 counter-skill: pre-built "pre-empt their next move" instruction (with
    # the opponent's already-existing predicted line embedded). Appended LAST so
    # it dominates the turn. No model call produced this — it's a prompt prime.
    if counter_context:
        sys_parts.append(str(counter_context))

    # Recent shared transcript window.
    window = _argument_transcript(transcript)[-TRANSCRIPT_WINDOW:]
    if window:
        convo = []
        for u in window:
            who = name_lookup.get(u.get("actor_id", ""), u.get("actor_id", "?"))
            convo.append(f"{who}: {u.get('text','')}")
        history = "Recent exchange:\n" + "\n".join(convo)
    else:
        history = (
            "You speak first. Open with a concrete claim and support. Do not mention "
            "that no opponent has spoken."
        )

    user = history + "\n\nNow give your argument."
    return [
        {"role": "system", "content": " ".join(sys_parts)},
        {"role": "user", "content": user},
    ]


async def _generate_utterance_traced(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
    counter_context: str | None = None,
) -> tuple[str, bool, str | None]:
    """Non-streaming generation that also reports the fallback outcome.

    Returns ``(text, used_fallback, fallback_reason)`` where ``fallback_reason`` is
    "timeout" (the per-call budget fired) or "empty" (model returned nothing /
    errored). Used by the headless self-play path so the latency metrics + spike
    can attribute fallbacks. ``_generate_utterance`` wraps this for callers that
    only want the text. ``counter_context`` (WS-5) is an optional pre-built
    prompt prime (the counter-skill's pre-empt instruction); it adds NO extra
    call — it's folded into this turn's single completion.
    """
    messages = _build_actor_messages(
        actor, topic, transcript, action, memories, name_lookup, counter_context
    )
    reason: str | None = None
    try:
        text = await gateway.complete(
            messages,
            model=_actor_model(actor),
            temperature=0.8,
            max_tokens=_action_max_tokens(action),
            timeout=_actor_timeout(),
        )
        text = (text or "").strip()
        if not text:
            reason = "empty"
    except asyncio.TimeoutError:  # per-call budget fired — slow/stalled model
        text = ""
        reason = "timeout"
    except Exception:  # noqa: BLE001 — stalled/failed model: fall back to real text
        text = ""
        reason = "empty"
    used_fallback = not text
    if used_fallback:
        text = _fallback_argument(actor, topic, turn_seed=len(transcript))
    cleaned = _sanitize(text)
    if not cleaned:
        used_fallback = True
        reason = reason or "empty"
        cleaned = _sanitize(_fallback_argument(actor, topic, turn_seed=len(transcript)))
    cleaned = _finalize_actor_text(cleaned, actor)
    return cleaned, used_fallback, reason


async def _generate_utterance(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
    counter_context: str | None = None,
) -> str:
    text, _fb, _reason = await _generate_utterance_traced(
        actor, topic, transcript, action, memories, name_lookup, counter_context
    )
    return text


def _sanitize(text: str) -> str:
    """Normalize model text into the compact battle-utterance contract."""
    return sanitize_battle_utterance(text)


def _finalize_actor_text(text: str, actor: Combatant) -> str:
    """Apply final low-latency shape fixes for generated actor turns."""
    return ensure_battle_sentence_floor(_sanitize(text), role=actor.role)


def _action_max_tokens(action: dict[str, Any]) -> int:
    """Return the model token budget after one-turn output-limit statuses."""
    base = _actor_max_tokens()
    try:
        requested = int(action.get("max_tokens") or base)
    except (TypeError, ValueError):
        requested = base
    return max(32, min(base, requested))


def _looks_like_heading(line: str) -> bool:
    """Detect decorative headings emitted despite the no-markdown prompt."""
    if len(line) > 90:
        return False
    lowered = line.lower().strip(":")
    if lowered.startswith(("against ", "for ")):
        return not re.search(r"[.!?]$", line)
    return lowered in {"argument", "claim", "support", "evidence", "rebuttal"}


# First-token wall-clock guard for the live streaming path. On a contended CPU
# the dominant risk is a stalled model that never emits a first token; we'd rather
# fail this one utterance (and fall back to a REAL templated argument) than hang
# the whole WS round. This uses the SMALL `first_token_timeout_s` (~8-15s) — NOT
# the larger non-streaming `llm_call_timeout_s` — so a cold/slow-to-start model
# falls back fast. Tokens after the first stream freely.
#
# WS-4: once a model is confirmed WARM (prewarmed at encounter create, resident in
# Ollama via keep_alive), we WIDEN this budget to `first_token_timeout_warm_s`. A
# warm model that's merely busy (CPU contention) is given more room to emit its
# first token before we fail over — this LOWERS the fallback rate without the cold
# hang, because the wider budget applies ONLY to a model proven not-cold. A cold or
# never-prewarmed model keeps the small guard.
def _first_token_timeout(model: str | None = None) -> float:
    cold = float(getattr(settings, "first_token_timeout_s", 8) or 8)
    if model is not None and is_model_warm(model):
        warm = float(getattr(settings, "first_token_timeout_warm_s", 0) or 0)
        if warm > cold:
            return warm
    return cold


STREAM_FIRST_TOKEN_TIMEOUT_S = _first_token_timeout()


async def _stream_utterance(
    actor: Combatant,
    topic: str,
    transcript: list[dict[str, Any]],
    action: dict[str, Any],
    memories: list[str],
    name_lookup: dict[str, str],
    counter_context: str | None = None,
):
    """Live streaming twin of `_generate_utterance`.

    Async generator that REUSES `_build_actor_messages` for the prompt, iterates
    `gateway.stream(...)`, sanitizes every token via `_sanitize`, and accumulates
    the full text. Each yield is a dict:

      * `{"kind": "token", "text": <sanitized chunk>}` per streamed chunk, and
      * exactly one terminating `{"kind": "done", "text": <full accumulated text>,
        "fallback": <bool>, "fallback_reason": <"timeout"|"empty"|None>}`
        carrying the canonical assembled utterance (with the same templated
        fallback as `_generate_utterance` if the stream produced nothing). The
        `fallback`/`fallback_reason` fields let callers (latency metrics) record
        WHY a turn fell back without re-deriving it.

    A first-token timeout (`_first_token_timeout()`, the small
    `settings.first_token_timeout_s`) guards a stalled CPU model: if no token
    arrives in time the utterance fails over to the fallback
    line instead of hanging the round. `_generate_utterance` is intentionally
    left untouched so the headless/REST paths keep their determinism.

    ``counter_context`` (WS-5) is an optional pre-built prompt prime for the
    Rhetorical Flourish counter-skill — it is folded into THIS turn's prompt and
    adds NO extra model call (the stream is still one stream).
    """
    messages = _build_actor_messages(
        actor, topic, transcript, action, memories, name_lookup, counter_context
    )
    model = _actor_model(actor)
    # WS-4: the first-token budget widens for a warm (prewarmed/resident) model.
    first_token_budget = _first_token_timeout(model)
    parts: list[str] = []
    fallback_reason: str | None = None
    try:
        agen = gateway.stream(
            messages, model=model, temperature=0.8,
            max_tokens=_action_max_tokens(action),
        )
        first = True
        while True:
            try:
                if first:
                    chunk = await asyncio.wait_for(
                        agen.__anext__(), timeout=first_token_budget
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
    except asyncio.TimeoutError:  # first-token guard fired — slow/stalled model
        fallback_reason = "timeout"
        parts = []
    except Exception:  # noqa: BLE001 — errored stream
        fallback_reason = "empty"
        parts = []

    full = "".join(parts).strip()
    used_fallback = False
    if not full:
        # Distinguish a clean-but-empty stream ("empty") from a timed-out one.
        if fallback_reason is None:
            fallback_reason = "empty"
        used_fallback = True
        full = _fallback_argument(actor, topic, turn_seed=len(transcript))
    else:
        fallback_reason = None
    full = _sanitize(full)
    if not full:
        used_fallback = True
        if fallback_reason is None:
            fallback_reason = "empty"
        full = _sanitize(_fallback_argument(actor, topic, turn_seed=len(transcript)))
    raw_full = full
    full = _finalize_actor_text(full, actor)
    if full != raw_full:
        suffix = full[len(raw_full):] if full.startswith(raw_full) else full
        if suffix:
            yield {"kind": "token", "text": suffix}
    yield {
        "kind": "done",
        "text": full,
        "fallback": used_fallback,
        "fallback_reason": fallback_reason,
    }


# ---- Core round logic (shared) ----------------------------------------------


def _apply_round_damage(
    combatants: list[Combatant],
    scored: list[tuple],
    momentum: dict[str, float],
    topic: str = "",
) -> list[dict[str, Any]]:
    """Apply cycle-winner damage from a round's scored utterances.

    The judge still scores every utterance, but only the side with the stronger
    average score damages the opposing side. That winner's highest-scoring actor
    produces one damage packet, split across living defenders.

    Each `scored` entry is `(actor, score, rationale)` and may carry optional
    dicts: the 4th element for extra verdict fields (e.g. why/logic/persuasion),
    and the 5th for damage metadata (`attack_type`, `skill_mult`). Older
    3-tuple callers stay supported.

    ``topic`` (gacha wave) flows into ``domain_match_mult`` so a domain-aligned
    attacker (e.g. SCIENCE monster on a SCIENCE topic) gets the +20% bonus and a
    mismatched attacker the -10% penalty. Default ``""`` reduces to 1.0.
    """
    entries: list[tuple[Combatant, float, str, dict[str, Any], dict[str, Any]]] = []
    side_scores: dict[str, list[float]] = {"party": [], "enemy": []}
    targets_by_actor: dict[str, list[Combatant]] = {}
    for entry in scored:
        actor, score, rationale = entry[0], entry[1], entry[2]
        extra: dict[str, Any] = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
        damage_meta: dict[str, Any] = (
            entry[4] if len(entry) > 4 and isinstance(entry[4], dict) else {}
        )
        score = float(score)
        entries.append((actor, score, str(rationale), extra, damage_meta))
        if actor.role in side_scores:
            side_scores[actor.role].append(score)
        enemy_role = "enemy" if actor.role == "party" else "party"
        targets_by_actor[actor.monster_id] = [
            c for c in combatants if c.role == enemy_role and c.alive
        ]

    winner_role = _cycle_winner_role(side_scores)
    winning_entry = _cycle_winning_entry(entries, winner_role)
    damage_actor_id = winning_entry[0].monster_id if winning_entry else None
    verdicts: list[dict[str, Any]] = []

    for actor, score, rationale, extra, damage_meta in entries:
        targets = targets_by_actor.get(actor.monster_id, [])
        target_id = targets[0].monster_id if targets else None
        total_dmg = 0
        if actor.monster_id != damage_actor_id or not targets:
            verdicts.append(
                {
                    "actor_id": actor.monster_id,
                    "target": target_id,
                    "score": score,
                    "rationale": rationale,
                    "damage": 0,
                    **extra,
                }
            )
            continue
        mom = momentum.get(actor.role, 1.0)
        dmatch = domain_match_mult(getattr(actor, "domain", None) or "GENERAL", topic)
        attack_type = str(damage_meta.get("attack_type") or actor.type)
        skill_mult = float(damage_meta.get("skill_mult", 1.0) or 1.0)
        target_defense_mult = float(damage_meta.get("target_defense_mult", 1.0) or 1.0)
        damage_score = _cycle_damage_score(actor.role, score, side_scores)
        for target in targets:
            dmg = compute_damage(
                score=damage_score,
                attacker_type=attack_type,
                defender_type=target.type,
                skill_mult=skill_mult,
                momentum=mom,
                attacker_level=actor.level,
                defender_level=target.level,
                # Gacha wave: real ATK/DEF/domain feed the formula. Defaults on
                # Combatant keep pre-gacha numerical behavior for old callers.
                attacker_atk=getattr(actor, "atk", 10),
                defender_def=getattr(target, "def_", 10),
                domain_match=dmatch,
            )
            dmg = round(dmg * _battle_damage_multiplier())
            if target_defense_mult != 1.0:
                dmg = round(dmg * max(0.0, target_defense_mult))
            dmg = max(0, round(dmg / len(targets)))
            target.hp = max(0, target.hp - dmg)
            total_dmg += dmg
        verdicts.append(
            {
                "actor_id": actor.monster_id,
                "target": target_id,
                "score": score,
                "rationale": rationale,
                "damage": total_dmg,
                **extra,
            }
        )

    # Momentum update: winners of the round gain, losers lose. Clamp 0.7..1.3.
    party_avg = _side_average(side_scores["party"])
    enemy_avg = _side_average(side_scores["enemy"])
    if party_avg is not None and enemy_avg is not None:
        swing = (party_avg - enemy_avg) / 100.0
        momentum["party"] = max(0.7, min(1.3, momentum.get("party", 1.0) + swing * 0.15))
        momentum["enemy"] = max(0.7, min(1.3, momentum.get("enemy", 1.0) - swing * 0.15))
    else:
        for side in ("party", "enemy"):
            avg = _side_average(side_scores[side])
            if avg is None:
                continue
            swing = (avg - 50.0) / 100.0
            momentum[side] = max(0.7, min(1.3, momentum.get(side, 1.0) + swing * 0.15))

    return verdicts


def _reaction_utterances(
    combatants: list[Combatant],
    verdicts: list[dict[str, Any]],
    turn_no: int,
) -> list[dict[str, Any]]:
    """Build event-triggered personality lines after cycle damage resolves."""
    by_id = {c.monster_id: c for c in combatants}
    events: list[dict[str, Any]] = []
    for v in verdicts:
        if int(v.get("damage") or 0) <= 0:
            continue
        actor = by_id.get(str(v.get("actor_id")))
        target = by_id.get(str(v.get("target")))
        if actor is None or target is None:
            continue
        _append_reaction(events, actor, "deals_damage", turn_no, v)
        _append_reaction(events, target, "takes_damage", turn_no, v)
        low_state = _low_hp_reaction_state(target)
        if low_state:
            speaker = actor if actor.role != target.role else target
            _append_reaction(events, speaker, low_state, turn_no, v)
        break
    return events[:3]


def _append_reaction(
    out: list[dict[str, Any]],
    actor: Combatant,
    state: str,
    turn_no: int,
    verdict: dict[str, Any],
) -> None:
    text = _battle_reaction_text(actor, state, f"{turn_no}:{verdict}")
    if not text:
        return
    out.append(
        {
            "turn": turn_no,
            "actor_id": actor.monster_id,
            "actor_role": actor.role,
            "side": _side_for(actor),
            "skill_used": f"reaction:{state}",
            "reaction_state": state,
            "text": text,
            "ts": time.time(),
            **_event_timing(),
        }
    )


def _battle_reaction_text(actor: Combatant, state: str, seed: str) -> str:
    actor.persona = ensure_battle_reactions(
        actor.persona,
        actor.type,
        role=actor.role,
        fallback_name=actor.name,
    )
    return select_battle_reaction(
        actor.persona,
        state,
        debate_type=actor.type,
        role=actor.role,
        seed=seed,
    )


def _low_hp_reaction_state(target: Combatant) -> str | None:
    if not target.alive or target.max_hp <= 0:
        return None
    if target.hp > target.max_hp * CAPTURABLE_HP_FRACTION:
        return None
    return "enemy_low_hp" if target.role == "enemy" else "user_low_hp"


def _side_average(scores: list[float]) -> float | None:
    return (sum(scores) / len(scores)) if scores else None


def _cycle_winner_role(side_scores: dict[str, list[float]]) -> str | None:
    party_avg = _side_average(side_scores.get("party", []))
    enemy_avg = _side_average(side_scores.get("enemy", []))
    if party_avg is None or enemy_avg is None:
        return None
    if abs(party_avg - enemy_avg) < CYCLE_TIE_EPSILON:
        return None
    return "party" if party_avg > enemy_avg else "enemy"


def _cycle_damage_score(
    winner_role: str,
    winner_score: float,
    side_scores: dict[str, list[float]],
) -> float:
    opposing_role = "enemy" if winner_role == "party" else "party"
    opposing_avg = _side_average(side_scores.get(opposing_role, []))
    if opposing_avg is None:
        return winner_score
    margin = max(0.0, winner_score - opposing_avg)
    return max(50.0, min(100.0, 50.0 + CYCLE_DAMAGE_WIN_BONUS + margin * CYCLE_DAMAGE_MARGIN_MULT))


def _cycle_winning_entry(
    entries: list[tuple[Combatant, float, str, dict[str, Any], dict[str, Any]]],
    winner_role: str | None,
) -> tuple[Combatant, float, str, dict[str, Any], dict[str, Any]] | None:
    if winner_role is None:
        return None
    candidates = [entry for entry in entries if entry[0].role == winner_role]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry[1])


def _skill_cost_safe(skill_name: str | None) -> int:
    """Look up a skill's MP cost via skill_engine, never raising."""
    if not skill_name:
        return 0
    try:
        from app.debate.skill_engine import skill_cost as _sc  # type: ignore

        return int(_sc(skill_name))
    except Exception:  # noqa: BLE001
        return 0


async def _get_mp_safe(eid: str, monster_id: str, max_mp: int) -> int:
    """Read a single combatant's MP from Redis for the skill-affordability gate.

    FAIL-CLOSED on a genuine cache miss (WS-5 fix). The old behavior returned
    ``max_mp`` on ANY miss, so the moment the ``enc:{eid}:mp`` hash was evicted
    (TTL, restart, or a Redis blip) every skill became "free" — a counter-skill
    like Rhetorical Flourish would always be castable regardless of spend. That
    silently broke the MP economy post-eviction.

    Resolution, in priority order:
      * The monster's MP is present in the hash -> return it (clamped to
        ``[0, max_mp]``).
      * The hash exists and is NON-empty but this monster isn't in it yet -> a
        legitimate fresh-combatant state; default to ``max_mp`` (full pool).
      * The hash is EMPTY (evicted / never seeded) or Redis errors -> a genuine
        miss: return ``0`` so an MP-costed skill is DENIED rather than freely
        granted. Free skills (cost 0) still pass their own gate, so this never
        blocks a normal turn.
    """
    try:
        from app.redis_state import get_mp_map

        m = await get_mp_map(eid)
        if monster_id in m:
            return max(0, min(int(m[monster_id]), int(max_mp)))
        # Populated hash without this monster -> fresh combatant, full pool.
        if m:
            return int(max_mp)
        # Empty hash -> evicted / never seeded: deny MP-costed skills.
        return 0
    except Exception:  # noqa: BLE001 — Redis down/blip is a genuine miss: fail closed.
        return 0


async def _set_mp_safe(eid: str, monster_id: str, mp: int) -> None:
    try:
        from app.redis_state import set_mp

        await set_mp(eid, monster_id, max(0, int(mp)))
    except Exception:  # noqa: BLE001
        return


async def _regen_mp_and_emit(eid: str, combatants: list[Combatant]):
    """Apply +``MP_REGEN_PER_ROUND`` to every combatant's MP and yield events.

    Reads current MP from ``enc:{eid}:mp`` (Redis hash), clamps the new value to
    each combatant's ``max_mp``, writes it back, and yields one
    ``Event("mp", {monster_id, mp, max_mp})`` per combatant so WS clients can
    update the blue MP bar in lockstep with HP.

    Best-effort: any Redis failure swallows the error and skips emission so a
    transient cache hiccup never ends the round.
    """
    try:
        from app.redis_state import get_mp_map, set_mp

        cur = await get_mp_map(eid)
        for c in combatants:
            try:
                start = int(cur.get(c.monster_id, c.max_mp))
            except (TypeError, ValueError):
                start = c.max_mp
            new = min(c.max_mp, start + MP_REGEN_PER_ROUND)
            await set_mp(eid, c.monster_id, new)
            yield Event("mp", {"monster_id": c.monster_id, "mp": new, "max_mp": c.max_mp})
    except Exception:  # noqa: BLE001 — never break the round on a Redis blip
        return


def _phase_for(combatants: list[Combatant]) -> tuple[str, list[str]]:
    """Compute phase + capturable wild ids from current HP."""
    party_alive = any(c.alive for c in combatants if c.role == "party")
    enemy_alive = any(c.alive for c in combatants if c.role == "enemy")
    if not party_alive and not enemy_alive:
        return "lost", []
    if not party_alive:
        return "lost", []
    if not enemy_alive:
        return "won", []
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
    damage_meta_by_id: dict[str, dict[str, Any]] = {}

    # WS-0-LAT: time the whole round + each utterance generation, and record
    # whether (and why) a templated fallback was used. Negligible overhead; emits
    # one structured `battle.latency` log line at the end of the round.
    rt = RoundTimer.start(eid, round_no=start_turn)

    # --- Speaking pass (sequential so each actor sees prior utterances) ---
    for actor in order:
        if not actor.alive:
            continue
        actor_started = time.monotonic()
        turn_no += 1
        battle_state = _build_battle_state(
            actor, combatants, topic, turn_no, last_verdict_score, momentum
        )
        action = _decide_action(actor, battle_state)
        skill_name, attack_type, skill_power = _resolve_skill(actor, action.get("skill"))
        if skill_name:
            cost = _skill_cost_safe(skill_name)
            if cost > 0:
                cur_mp = await _get_mp_safe(eid, actor.monster_id, actor.max_mp)
                if cur_mp < cost:
                    skill_name = None
                    attack_type = actor.type
                    skill_power = 1.0
                else:
                    await _set_mp_safe(eid, actor.monster_id, cur_mp - cost)
                    yield Event(
                        "mp",
                        {
                            "monster_id": actor.monster_id,
                            "mp": cur_mp - cost,
                            "max_mp": actor.max_mp,
                            **_event_timing(actor_started),
                        },
                    )
        action = {**action, "skill": skill_name}
        damage_meta_by_id[actor.monster_id] = {
            "attack_type": attack_type,
            "skill_mult": skill_power,
        }
        memories = await _gather_memories(actor, topic, run_id)
        transcript = await get_transcript_safe(eid)

        # WS-5 counter-skill: when the resolved skill is Rhetorical Flourish (and
        # was paid for above — skill_name survives the MP gate), prime this turn
        # with the opponent's predicted next line. ZERO extra model calls: the
        # line is the most recent opposing utterance ALREADY in `transcript`, or
        # on round 1 the enemy's already-materialized opening (a cache lookup).
        counter_context = await _resolve_counter_context(
            actor, skill_name, transcript, topic, combatants
        )

        # Live token streaming: emit additive `token` events as the model thinks,
        # then the canonical `utterance` event with the full assembled text. The
        # `done` chunk carries the fallback-resolved text, so a stalled/empty
        # stream still produces a complete utterance (whole-utterance fallback).
        text = ""
        used_fallback = False
        fallback_reason: str | None = None
        with rt.utterance(actor.monster_id, actor.role, actor.side):
            async for chunk in _stream_utterance(
                actor, topic, transcript, action, memories, name_lookup,
                counter_context=counter_context,
            ):
                if chunk["kind"] == "token":
                    yield Event(
                        "token",
                        {
                            "turn": turn_no,
                            "actor_id": actor.monster_id,
                            "side": actor.side,
                            "text": chunk["text"],
                            **_event_timing(actor_started),
                        },
                    )
                else:  # "done"
                    text = chunk["text"]
                    used_fallback = bool(chunk.get("fallback"))
                    fallback_reason = chunk.get("fallback_reason")
            if used_fallback:
                rt.utterances[-1].mark_fallback(fallback_reason or "empty")

        utt = {
            "turn": turn_no,
            "actor_id": actor.monster_id,
            "actor_role": actor.role,
            "side": actor.side,
            "skill_used": action.get("skill"),
            "text": text,
            "ts": time.time(),
            **_event_timing(actor_started),
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
            scored.append((
                actor,
                js.score,
                js.rationale,
                extra,
                damage_meta_by_id.get(actor.monster_id, {}),
            ))

    verdicts = _apply_round_damage(combatants, scored, momentum, topic=topic)

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

    for reaction in _reaction_utterances(combatants, verdicts, turn_no):
        await append_utterance(eid, reaction)
        yield Event("utterance", reaction)

    for c in combatants:
        await set_hp(eid, c.monster_id, c.hp)
        # Emit ONE HpUpdate per combatant ({monster_id, hp, max_hp}) — the frontend
        # patches combatants by monster_id, so a {id: hp} map silently no-ops (HP bug).
        yield Event("hp", {"monster_id": c.monster_id, "hp": c.hp, "max_hp": c.max_hp})

    # Gacha Wave B: end-of-round MP regen (+10, clamped to max_mp) for every
    # combatant. Emits per-combatant `mp` events so the WS clients can paint the
    # blue MP bar in lockstep with HP without a separate poll.
    async for mp_ev in _regen_mp_and_emit(eid, combatants):
        yield mp_ev

    # WS-0-LAT: emit the structured round-latency line (round_ms, per-actor gen_ms,
    # fallback count + reasons). Best-effort — never raises into the round.
    rt.finish()

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
    """Highest-initiative alive combatant on a side.

    The player's chosen avatar is always the party lead while it is alive (it is
    the run's permanent "main character"); otherwise fall back to level desc,
    then name. Enemies never carry the avatar flag, so the enemy lead is
    unchanged.
    """
    alive = [c for c in combatants if c.role == role and c.alive]
    if not alive:
        return None
    return sorted(alive, key=lambda c: (not c.is_avatar, -c.level, c.name))[0]


def _active_party(
    combatants: list[Combatant],
    active_party_id: str | None = None,
) -> Optional[Combatant]:
    """Return the player-selected living party agent, falling back to the lead."""
    if active_party_id:
        selected = next(
            (
                c
                for c in combatants
                if c.role == "party" and c.alive and c.monster_id == active_party_id
            ),
            None,
        )
        if selected is not None:
            return selected
    return _lead(combatants, "party")


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
    active_party_id: str | None = None,
):
    """Async generator running ONE human-driven round. Yields Event objects.

    The player's typed argument is the lead party monster's turn; the lead enemy
    rebuts autonomously. RESPONSIVENESS + CLARITY fixes:

      * STREAMED enemy rebuttal — the enemy uses `_stream_utterance` (same token
        mechanism as the auto round), so the WS emits `token` events as they arrive
        and perceived latency is first-token, not full generation.
      * COMBINED judging — both completed turns are scored in one judge call, and
        only the cycle winner's side inflicts HP damage. The losing verdict still
        emits with damage=0 so the UI can show both scores without a second hit.
      * CLEAR SIDES — the player's lead argues FOR the topic, the enemy lead argues
        AGAINST it (deterministic). `side` is threaded into both prompts and carried
        on the emitted utterance/token events.

    Winner-only damage applies (the player's chosen skill scales it if they win),
    then hp/phase emit.
    Mirrors `run_round_stream`'s event protocol so the WS/REST callers are
    unchanged. Mutates `combatants` HP and `momentum` in place.
    """
    from app.redis_state import (
        ENCOUNTER_TTL_SECONDS,
        append_effect,
        append_utterance,
        clear_effects,
        get_redis,
        k_judge,
        set_hp,
    )

    name_lookup = {c.monster_id: c.name for c in combatants}
    turn_no = start_turn

    player = _active_party(combatants, active_party_id)
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

    # WS-0-LAT: round latency instrumentation. The player turn is human-typed (no
    # model fallback), so we only time the ENEMY generation + judge here.
    rt = RoundTimer.start(eid, round_no=start_turn)

    skill_name, attack_type, skill_power = _resolve_skill(player, skill_id)
    player_started = time.monotonic()

    # Gacha Wave B: skill MP gate. If the player selected a skill they cannot
    # afford, strip the skill from THIS turn (the text still goes through —
    # punishing the player with a dead turn would feel awful). Deduct on use.
    if skill_name:
        cost = _skill_cost_safe(skill_name)
        if cost > 0:
            cur_mp = await _get_mp_safe(eid, player.monster_id, player.max_mp)
            if cur_mp < cost:
                skill_name = None
                attack_type = player.type
                skill_power = 1.0
            else:
                await _set_mp_safe(eid, player.monster_id, cur_mp - cost)
                yield Event(
                    "mp",
                    {
                        "monster_id": player.monster_id,
                        "mp": cur_mp - cost,
                        "max_mp": player.max_mp,
                        **_event_timing(player_started),
                    },
                )

    skill_spec = _resolved_skill_spec(player, skill_name)
    effect_kind = str(skill_spec.get("effect_kind") or "")
    judge_score_delta = 0.0
    enemy_status_contract = ""
    enemy_max_tokens: int | None = None
    target_defense_mult = 1.0

    if skill_name and skill_spec:
        skill_power *= _modifier_float(skill_spec, "damage_mult", 1.0)
        prompt_bonus = str(_skill_modifiers(skill_spec).get("prompt_bonus", "") or "")
        if effect_kind == "prompt_augment" and not prompt_bonus:
            prompt_bonus = "Lean into the selected skill's angle; reward a concrete, on-topic execution."
        if effect_kind == "judge_sway":
            judge_score_delta = _modifier_float(skill_spec, "score_delta", 3.0)
        if effect_kind == "defense":
            target_defense_mult = _modifier_float(skill_spec, "defense_mult", 0.75)
        if effect_kind == "status":
            sent_limit = _modifier_int(skill_spec, "enemy_sentence_limit", 0)
            max_tokens = _modifier_int(skill_spec, "enemy_max_tokens", 0)
            if sent_limit > 0:
                enemy_status_contract = (
                    f"You are constrained by {skill_name}; output exactly {sent_limit} "
                    "short sentence and no extra setup."
                )
            if max_tokens > 0:
                enemy_max_tokens = max_tokens
        message = {
            "prompt_augment": f"{player.name}'s next argument is amplified by {skill_name}.",
            "judge_sway": f"{skill_name} will visibly sway the judge this turn.",
            "defense": f"{player.name} braces with {skill_name}; incoming damage is reduced this turn.",
            "status": f"{skill_name} disrupts {enemy.name}'s next response.",
            "agent_argument": f"{player.name} attacks with {skill_name}.",
        }.get(effect_kind, f"{player.name} uses {skill_name}.")
        effect_payload = _effect_payload(
            skill=skill_spec,
            source=player,
            target=enemy,
            message=message,
            turn_no=start_turn + 1,
        )
        await append_effect(eid, effect_payload)
        yield Event("skill_effect", effect_payload)
        if int(skill_spec.get("duration_turns", 0) or 0) > 0:
            yield Event("status", effect_payload)

    fallback_model = next((c.model for c in combatants if c.model), None)

    # --- Player turn (human-typed or skill-generated) ---
    turn_no += 1
    cleaned_player_text = _sanitize((player_text or "").strip())
    generated_skill_turn = bool(
        skill_name and not cleaned_player_text and effect_kind == "agent_argument"
    )
    text = cleaned_player_text

    if generated_skill_turn:
        battle_state = _build_battle_state(
            player, combatants, topic, turn_no, last_verdict_score=50.0, momentum=momentum
        )
        action = {
            **_decide_action(player, battle_state),
            "behavior": (
                "argue on the player's behalf with this selected memory skill; "
                "attack using the Redis encounter transcript and agent memories"
            ),
            "skill": skill_name,
            "target": enemy.monster_id,
        }
        memories = await _gather_memories(player, topic, run_id)
        transcript = await get_transcript_safe(eid)
        used_fallback = False
        fallback_reason: str | None = None
        with rt.utterance(player.monster_id, player.role, player.side):
            async for chunk in _stream_utterance(
                player, topic, transcript, action, memories, name_lookup
            ):
                if chunk["kind"] == "token":
                    yield Event(
                        "token",
                        {
                            "turn": turn_no,
                            "actor_id": player.monster_id,
                            "actor_role": "party",
                            "side": player.side,
                            "text": chunk["text"],
                            **_event_timing(player_started),
                        },
                    )
                else:
                    text = chunk["text"]
                    used_fallback = bool(chunk.get("fallback"))
                    fallback_reason = chunk.get("fallback_reason")
            if used_fallback:
                rt.utterances[-1].mark_fallback(fallback_reason or "empty")

    text = text or f"({player.name} stays silent on {topic}.)"
    player_utt = {
        "turn": turn_no,
        "actor_id": player.monster_id,
        "actor_role": "party",
        "side": player.side,
        "skill_used": skill_name,
        "text": text,
        "ts": time.time(),
        **_event_timing(player_started),
    }
    await append_utterance(eid, player_utt)
    yield Event("utterance", player_utt)

    # A4 — OPTIMISTIC JUDGE. Emit an INSTANT heuristic display score for the
    # player's argument so the UI shows feedback <200ms after submit, well before
    # the slot-bound LLM judge returns. This is a DISPLAY-ONLY estimate: it does
    # NOT drive HP damage (damage is computed from the LLM score below and applied
    # only once, in a single animation). The front-end settles this estimate to
    # the authoritative `verdict` score when it arrives (matched by turn+actor_id).
    yield Event(
        "estimate",
        {
            "turn": turn_no,
            "actor_id": player.monster_id,
            "side": player.side,
            "score": heuristic_score(topic, text),
            **_event_timing(player_started),
        },
    )

    enemy_turn = turn_no + 1
    enemy_started = time.monotonic()

    # --- Enemy rebuttal (autonomous) ---
    battle_state = _build_battle_state(enemy, combatants, topic, enemy_turn, 50.0, momentum)
    action = _decide_action(enemy, battle_state)
    if enemy_status_contract:
        action = {**action, "status_contract": enemy_status_contract}
        if enemy_max_tokens is not None:
            action["max_tokens"] = enemy_max_tokens
    memories = await _gather_memories(enemy, topic, run_id)
    transcript = await get_transcript_safe(eid)

    # WS-5 counter-skill: if the enemy's own action resolves to Rhetorical Flourish,
    # prime its rebuttal with the PLAYER's predicted next move (the just-submitted
    # player line is already in `transcript`). ZERO extra model calls — pure scan.
    enemy_counter_context = await _resolve_counter_context(
        enemy, action.get("skill"), transcript, topic, combatants
    )

    # A1/A2 — MATERIALIZED OPENING. On the FIRST round the enemy's line is its
    # opening (arguing AGAINST the topic), which is player-independent and thus
    # cacheable. Use the cached/pre-generated opening instead of a live stream so
    # the first enemy turn is a pure retrieval (hit) or a single store-on-miss.
    # Only the lead enemy uses a skill action; the opening ignores transcript, so
    # the opening path is safe only on the very first round (no prior exchange to
    # rebut). Later rounds fall through to the live streaming rebuttal below.
    enemy_text = ""
    enemy_fallback = False
    enemy_fallback_reason: str | None = None
    is_opening = start_turn == 0 and len(transcript) <= 1  # only the player's turn so far
    with rt.utterance(enemy.monster_id, enemy.role, enemy.side) as enemy_metric:
        if is_opening:
            from app.debate.materialize import get_or_create_opening

            enemy_text, _hit = await get_or_create_opening(topic, enemy.model)
            enemy_text = _finalize_actor_text(enemy_text, enemy)
            # Emit the materialized opening as a single token so WS clients still get
            # the streamed-text event shape (no per-token cadence, but instant).
            yield Event(
                "token",
                {
                    "turn": enemy_turn,
                    "actor_id": enemy.monster_id,
                    "side": enemy.side,
                    "text": enemy_text,
                    **_event_timing(enemy_started),
                },
            )
        else:
            try:
                async for chunk in _stream_utterance(
                    enemy, topic, transcript, action, memories, name_lookup,
                    counter_context=enemy_counter_context,
                ):
                    if chunk["kind"] == "token":
                        yield Event(
                            "token",
                            {
                                "turn": enemy_turn,
                                "actor_id": enemy.monster_id,
                                "side": enemy.side,
                                "text": chunk["text"],
                                **_event_timing(enemy_started),
                            },
                        )
                    else:  # "done"
                        enemy_text = chunk["text"]
                        enemy_fallback = bool(chunk.get("fallback"))
                        enemy_fallback_reason = chunk.get("fallback_reason")
            except Exception:  # noqa: BLE001 — never let a stream error orphan the judge task
                if not enemy_text:
                    enemy_text = _fallback_argument(enemy, topic, turn_seed=len(transcript))
                    enemy_fallback = True
                    enemy_fallback_reason = "empty"
        if enemy_fallback:
            enemy_metric.mark_fallback(enemy_fallback_reason or "empty")

    turn_no = enemy_turn
    enemy_utt = {
        "turn": turn_no,
        "actor_id": enemy.monster_id,
        "actor_role": "enemy",
        "side": enemy.side,
        "skill_used": action.get("skill"),
        "text": enemy_text,
        "ts": time.time(),
        **_event_timing(enemy_started),
    }
    await append_utterance(eid, enemy_utt)
    yield Event("utterance", enemy_utt)

    # --- Judge BOTH utterances in ONE call. (Was two concurrent score_round calls,
    # but the enemy almost always falls back instantly on a cold local model, so the
    # "player-judge overlaps enemy generation" optimization rarely paid off — meanwhile
    # the SECOND judge call doubled local judge latency, the measured bottleneck. One
    # combined call halves it. score_round maps results back by actor_id, so per-actor
    # damage attribution is preserved.) ---
    #
    # A4 — RECONCILIATION DEADLINE. Autoplan finding: on a stalled single-slot
    # Ollama, score_round serially times out each candidate (~28-56s), so HP would
    # dangle long after the optimistic `estimate` already rendered. Cap the LLM
    # judge at `_human_judge_deadline()`; on timeout, COMMIT the already-displayed
    # heuristic score as authoritative (same heuristic the estimate used) so damage
    # is computed from it and the normal verdict+hp still emit — the round never
    # dangles. The non-human auto-round path (run_round_stream) is unchanged.
    judge_items = [
        {"actor_id": player.monster_id, "text": text},
        {"actor_id": enemy.monster_id, "text": enemy_text},
    ]
    judge_fallback = False
    with rt.utterance("__judge__", "judge", None) as judge_metric:
        try:
            scores = await asyncio.wait_for(
                score_round(topic, judge_items, fallback_model=fallback_model),
                timeout=_human_judge_deadline(),
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — stalled/failed judge
            # Settle from the heuristic so HP commits within the deadline. This is the
            # SAME score already shown to the player by the optimistic `estimate`, so
            # the displayed feedback is now authoritative rather than dangling.
            from app.debate.judge import JudgeScore

            judge_fallback = True
            scores = [
                JudgeScore(
                    actor_id=it["actor_id"],
                    score=heuristic_score(topic, it["text"]),
                    rationale="Heuristic score (judge deadline reached).",
                )
                for it in judge_items
            ]
        if judge_fallback:
            judge_metric.mark_fallback("judge_deadline")
    score_by_id = {js.actor_id: js for js in scores}
    if judge_score_delta and player.monster_id in score_by_id:
        js = score_by_id[player.monster_id]
        before = float(js.score)
        js.score = max(0.0, min(100.0, before + judge_score_delta))
        delta_label = f"+{judge_score_delta:g}" if judge_score_delta >= 0 else f"{judge_score_delta:g}"
        js.rationale = (
            f"{js.rationale} Visible judge modifier from {skill_name}: {delta_label}."
        ).strip()
        if getattr(js, "why", ""):
            js.why = f"{js.why} ({skill_name}: {delta_label})"

    # --- Apply cycle-winner damage. The judge scores both turns, but only the
    # higher-scoring side's turn mutates HP; the other verdict carries damage=0.
    scored: list[tuple] = []
    for actor, atk_type, mult in (
        (player, attack_type, skill_power),
        (enemy, enemy.type, 1.0),
    ):
        js = score_by_id.get(actor.monster_id)
        if js is None:
            continue
        damage_meta = {"attack_type": atk_type, "skill_mult": mult}
        if actor.monster_id == enemy.monster_id and target_defense_mult != 1.0:
            damage_meta["target_defense_mult"] = target_defense_mult
        scored.append((
            actor,
            js.score,
            js.rationale,
            {},
            damage_meta,
        ))
    verdicts = [
        {**v, "turn": turn_no}
        for v in _apply_round_damage(combatants, scored, momentum, topic=topic)
    ]

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

    for reaction in _reaction_utterances(combatants, verdicts, turn_no):
        await append_utterance(eid, reaction)
        yield Event("utterance", reaction)

    for c in combatants:
        await set_hp(eid, c.monster_id, c.hp)
        # Emit ONE HpUpdate per combatant ({monster_id, hp, max_hp}) — the frontend
        # patches combatants by monster_id, so a {id: hp} map silently no-ops (HP bug).
        yield Event("hp", {"monster_id": c.monster_id, "hp": c.hp, "max_hp": c.max_hp})

    # Gacha Wave B: same +10 MP regen at end-of-round as the autonomous path so
    # the human player and the AI use the exact same MP economy.
    async for mp_ev in _regen_mp_and_emit(eid, combatants):
        yield mp_ev

    # WS-0-LAT: emit the structured round-latency line for the human round.
    rt.finish()

    await clear_effects(eid)

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
    # Gacha-wave stats. Accept both attribute names (`def_` from the ORM, `def`
    # from a plain dict / schema mirror) and fall back to neutral defaults so
    # pre-gacha callers keep working.
    def_val = g("def_", None)
    if def_val is None:
        def_val = g("def", 10)
    return Combatant(
        monster_id=str(g("id", f"{role}-{int(time.time()*1000)}")),
        name=str(g("name", role.title())),
        type=str(mtype),
        role=role,
        hp=max_hp,
        max_hp=max_hp,
        level=int(g("level", 1) or 1),
        owner=str(owner),
        persona=ensure_battle_reactions(
            g("persona", {}) or {},
            mtype,
            role=role,
            fallback_name=str(g("name", role.title())),
        ),
        harness=normalize_harness(g("harness", {}) or {}, role=role),
        skills=list(g("skills", []) or []),
        model=g("model"),
        atk=int(g("atk", 10) or 10),
        def_=int(def_val or 10),
        max_mp=int(g("max_mp", 50) or 50),
        domain=str(g("domain", "GENERAL") or "GENERAL"),
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
        damage_meta_by_id: dict[str, dict[str, Any]] = {}

        # WS-0-LAT: time each headless round + record per-utterance fallbacks so the
        # go/no-go spike + WS-F training share the same latency instrumentation as
        # the live paths. (Headless uses the non-streaming `complete` path.)
        rt = RoundTimer.start(f"selfplay:{party.monster_id}", round_no=turn_no)

        for actor in order:
            if not actor.alive:
                continue
            turn_no += 1
            battle_state = _build_battle_state(
                actor, combatants, topic, turn_no, last_score, momentum
            )
            action = _decide_action(actor, battle_state)
            skill_name, attack_type, skill_power = _resolve_skill(actor, action.get("skill"))
            action = {**action, "skill": skill_name}
            damage_meta_by_id[actor.monster_id] = {
                "attack_type": attack_type,
                "skill_mult": skill_power,
            }
            memories: list[str] = []  # headless: skip RAG for determinism/speed
            # WS-5 counter-skill (headless/training): prime a generated turn with
            # the opponent's predicted next line from the in-memory transcript —
            # zero extra completions, same single `complete` call as any turn.
            counter_context = await _resolve_counter_context(
                actor, skill_name, transcript, topic, combatants
            )
            with rt.utterance(actor.monster_id, actor.role, _side_for(actor)) as _m:
                text, _used_fb, _fb_reason = await _generate_utterance_traced(
                    actor, topic, transcript, action, memories, name_lookup,
                    counter_context=counter_context,
                )
                if _used_fb:
                    _m.mark_fallback(_fb_reason or "empty")
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
        scored: list[tuple] = []
        for js in scores:
            a = actor_by_id.get(js.actor_id)
            if a:
                scored.append((
                    a,
                    js.score,
                    js.rationale,
                    {},
                    damage_meta_by_id.get(a.monster_id, {}),
                ))
                if a.role == "party":
                    last_score = js.score
        round_verdicts = _apply_round_damage(combatants, scored, momentum)
        for v in round_verdicts:
            verdicts.append({**v, "turn": turn_no})
        transcript.extend(_reaction_utterances(combatants, round_verdicts, turn_no))

        # WS-0-LAT: emit the structured round-latency line for this self-play round.
        rt.finish()

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
