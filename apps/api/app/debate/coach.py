"""ARGUE COPILOT — the player-first coaching engine (Agent 8).

The pivot: the PLAYER argues against the enemy; the lead PARTY monster acts as the
player's debate COACH. The player drafts a rough argument, the coach rewrites it
into a stronger version (keeping the player's intent + voice), and returns a short
rationale plus a suggested rhetorical angle.

The training -> better-help link
--------------------------------
The coach's "brain" IS the lead party monster's trained genome. We read the
monster's genome (`read_genome`) and assemble its `system_prompt(...)` — the exact
prompt DNA that GEPA/GRPO optimizes during training. A better-trained monster has
a sharper system prompt (directives, skill fragments, tone), so it produces a
sharper coaching prompt and therefore better suggestions. Training your monster
literally makes your copilot smarter. The monster's persona + (optionally) its
retrieved battle memories further shape the advice.

Robustness
----------
This module must NEVER raise into the request path. Encounter-state loading,
gateway calls, and parsing are all wrapped: any failure degrades to a graceful
fallback suggestion (the draft lightly cleaned + a generic coaching tip) so the
endpoint always returns a valid AssistResult and never 500s.
"""
from __future__ import annotations

from typing import Any, Optional

from app.schemas import AssistResult, AssistSuggestion

# Coaching generation knobs. Slightly lower temperature than a debater's turn —
# we want a faithful, sharpened rewrite of the player's intent, not a wild riff.
_COACH_TEMPERATURE = 0.6
_COACH_MAX_TOKENS = 320

# Marker the model uses to separate the rewrite from its coaching note. Kept
# trivially parseable so a small local model rarely breaks it.
_RATIONALE_MARKER = "RATIONALE:"
_ANGLE_MARKER = "ANGLE:"


async def coach_argument(
    session: Any,
    eid: str,
    draft: str,
    skill_id: Optional[str] = None,
) -> AssistResult:
    """Coach the player's drafted argument using the lead party monster's genome.

    Loads the live encounter (topic, combatants, run_id), finds the lead PARTY
    monster (the coach), builds a coaching prompt seeded with the coach's trained
    genome system prompt + persona, and asks the model for a stronger version of
    the player's draft plus a one-line rationale and a suggested angle.

    Never raises: any load/gateway/parse failure degrades to a graceful fallback
    suggestion so the caller always receives a valid AssistResult.
    """
    draft = (draft or "").strip()

    # --- Load encounter state (defensively) ---
    topic = ""
    run_id: Optional[str] = None
    coach = None
    enemy_last = ""
    try:
        from app.routers.encounter import get_meta, load_combatants
        from app.debate.orchestrator import _lead

        meta = await get_meta(eid)
        topic = meta.get("topic", "") or ""
        run_id = meta.get("run_id") or None
        combatants = await load_combatants(eid)
        coach = _lead(combatants, "party")
        enemy_last = await _last_enemy_argument(eid)
    except Exception:  # noqa: BLE001
        # Could not load state — still return a usable fallback below.
        coach = None

    coach_id = getattr(coach, "monster_id", None) if coach is not None else None

    # --- Optional: the coach's relevant battle memories sharpen the advice ---
    memories: list[str] = []
    if coach is not None:
        memories = await _gather_coach_memories(session, coach_id, topic)

    # --- Build the coaching prompt from the coach's TRAINED genome ---
    system_text = _coach_system_prompt(coach)
    user_text = _coach_user_prompt(topic, enemy_last, draft, skill_id, memories)

    # --- Generate (defensively) ---
    raw = ""
    try:
        from app.gateway.gateway import gateway

        model = getattr(coach, "model", None) if coach is not None else None
        raw = await gateway.complete(
            [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            model=model,
            temperature=_COACH_TEMPERATURE,
            max_tokens=_COACH_MAX_TOKENS,
        )
        raw = (raw or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""

    suggestion = _parse_suggestion(raw, draft, topic, skill_id)
    return AssistResult(
        encounter_id=eid,
        coach_monster_id=coach_id,
        suggestions=[suggestion],
    )


# ---- Prompt construction ----------------------------------------------------


def _coach_system_prompt(coach: Any) -> str:
    """Assemble the coach's system prompt from its TRAINED genome.

    This is the training->better-help seam: a better-trained monster has a richer
    genome system prompt, which yields better coaching. Falls back to a generic
    coach persona if the monster (or its genome) is unavailable.
    """
    genome_prompt = ""
    persona_line = ""
    name = "your monster"
    try:
        from app.training.genome import read_genome, system_prompt

        if coach is not None:
            genome = read_genome(coach)
            genome_prompt = system_prompt(genome) or ""
            name = getattr(coach, "name", None) or genome.get("persona", {}).get("name") or name
            persona_line = _persona_line(getattr(coach, "persona", {}) or {})
    except Exception:  # noqa: BLE001
        genome_prompt = ""

    parts: list[str] = []
    if genome_prompt:
        parts.append(genome_prompt)
    parts.append(
        f"You are {name}, but right now you are the PLAYER'S DEBATE COACH — not the "
        "debater. Your job is to take the player's rough argument and make it land "
        "harder against the enemy. Keep the player's core intent and voice; sharpen "
        "the logic and rhetoric; never invent facts. Apply the debating instincts "
        "above to the player's argument."
    )
    if persona_line:
        parts.append(f"Coaching personality — {persona_line}.")
    parts.append(
        "Respond in EXACTLY this format and nothing else:\n"
        "<the improved argument, 2-4 sentences, ready for the player to send>\n"
        f"{_RATIONALE_MARKER} <one short line on why this version is stronger>\n"
        f"{_ANGLE_MARKER} <2-4 word label for the rhetorical strategy you used>"
    )
    return "\n\n".join(parts)


def _coach_user_prompt(
    topic: str,
    enemy_last: str,
    draft: str,
    skill_id: Optional[str],
    memories: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"Debate topic: {topic or '(unspecified)'}")
    if enemy_last:
        lines.append(f"The enemy just argued: \"{enemy_last}\"")
    if skill_id:
        lines.append(f"The player wants to use the skill / angle: {skill_id}")
    if memories:
        lines.append("Relevant past battle experience: " + " | ".join(memories))
    if draft:
        lines.append(f"The player's rough draft argument:\n\"{draft}\"")
        lines.append(
            "Rewrite the draft into a stronger argument that rebuts the enemy and "
            "advances the player's side. Preserve the player's intent and voice."
        )
    else:
        lines.append(
            "The player has not written anything yet. Draft a strong opening "
            "argument for the player on this topic that rebuts the enemy if present."
        )
    return "\n\n".join(lines)


def _persona_line(persona: dict[str, Any]) -> str:
    bits = []
    for key in ("tone", "style", "voice"):
        if persona.get(key):
            bits.append(f"{key}: {persona[key]}")
    return "; ".join(bits)


# ---- Context helpers --------------------------------------------------------


async def _last_enemy_argument(eid: str) -> str:
    """Best-effort: pull the most recent enemy utterance from the live transcript.

    Defensive — never raises; returns "" if anything is unavailable.
    """
    try:
        from app.debate.orchestrator import get_transcript_safe

        transcript = await get_transcript_safe(eid)
        for utt in reversed(transcript or []):
            if utt.get("actor_role") == "enemy" and utt.get("text"):
                return str(utt["text"]).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _gather_coach_memories(
    session: Any, monster_id: Optional[str], topic: str
) -> list[str]:
    """Optionally inject the coach's relevant battle memories. Skips gracefully."""
    if not monster_id or session is None or not topic:
        return []
    try:
        from app.memory.retriever import retrieve

        res = await retrieve(session, monster_id, topic, k=2)
        out: list[str] = []
        for m in res or []:
            if isinstance(m, dict):
                out.append(str(m.get("summary") or m.get("content") or ""))
            elif isinstance(m, str):
                out.append(m)
        # Keep memory snippets short so they don't dominate the prompt.
        return [s[:240] for s in out if s][:2]
    except Exception:  # noqa: BLE001
        return []


# ---- Parsing + fallback -----------------------------------------------------


def _sanitize(text: str) -> str:
    """Strip control chars that break strict JSON parsers (browser JSON.parse)."""
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t").strip()


def _parse_suggestion(
    raw: str, draft: str, topic: str, skill_id: Optional[str]
) -> AssistSuggestion:
    """Parse the model output into an AssistSuggestion. Degrades gracefully if the
    model returned nothing or an unparseable blob."""
    raw = _sanitize(raw or "")
    if not raw:
        return _fallback_suggestion(draft, topic, skill_id)

    improved = raw
    rationale = ""
    angle = ""

    # Split off the ANGLE: line first, then the RATIONALE: line.
    if _ANGLE_MARKER in improved:
        improved, _, angle = improved.partition(_ANGLE_MARKER)
        angle = angle.strip().splitlines()[0].strip() if angle.strip() else ""
        improved = improved.strip()
    if _RATIONALE_MARKER in improved:
        improved, _, rationale = improved.partition(_RATIONALE_MARKER)
        rationale = rationale.strip()
        improved = improved.strip()

    improved = improved.strip().strip('"').strip()
    if not improved:
        return _fallback_suggestion(draft, topic, skill_id)

    if not rationale:
        rationale = "Sharpened the logic and made the point land harder."
    if not angle:
        angle = (skill_id or "direct rebuttal")

    return AssistSuggestion(
        improved=improved,
        rationale=rationale,
        skill_id=skill_id,
        angle=angle,
    )


def _fallback_suggestion(
    draft: str, topic: str, skill_id: Optional[str]
) -> AssistSuggestion:
    """A coach that can't reach the model still helps: lightly clean the draft (or
    seed an opener) and pair it with a generic, actionable coaching tip."""
    cleaned = _sanitize(draft or "")
    if cleaned:
        improved = cleaned
        if improved and improved[-1] not in ".?!":
            improved += "."
        rationale = (
            "Coach offline — lead with your strongest claim, back it with one "
            "concrete example, then end on a sharp question."
        )
    else:
        topic_str = topic or "this issue"
        improved = (
            f"The strongest case here is clear: on {topic_str}, the evidence and the "
            "stakes both favor my position. Concede nothing without reclaiming it, "
            "and make them answer the hardest question first."
        )
        rationale = (
            "Coach offline — opened with a confident frame; replace the placeholder "
            "with your specific point and one concrete example."
        )
    return AssistSuggestion(
        improved=improved,
        rationale=rationale,
        skill_id=skill_id,
        angle=(skill_id or "frame and anchor"),
    )
