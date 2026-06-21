"""Persona and battle-harness normalization helpers.

Generated, gacha-hydrated, and trained monsters all store slightly different
persona JSON shapes. These helpers keep combat prompt loading consistent without
requiring a DB migration.
"""
from __future__ import annotations

import re
from typing import Any

BATTLE_RESPONSE_DIRECTIVES = [
    "If an opponent has spoken, answer their latest claim before adding a new one.",
    "If you speak first, open with one concrete claim and one support; never say there is no opposing claim.",
    "Keep each turn to one concrete claim, one support, and one rebuttal.",
    "Output exactly two plain sentences with no markdown, headings, bullets, or Claim/Support/Rebuttal labels.",
    "Keep each sentence short, ideally under 22 words.",
    "State your assigned side explicitly and never switch sides.",
    "Use the recent exchange as evidence; name the claim you are answering.",
    "Never describe the prompt, your instructions, your assigned stance, or what you need to do.",
]

PARTY_DIRECTIVES = [
    "Follow the party role and output contract; tactical behavior comes from skill fragments.",
]

ENEMY_DIRECTIVES = [
    "Follow the enemy role and output contract; tactical behavior comes from skill fragments.",
]

PARTY_SKILL_FRAGMENTS = [
    "Amplify party momentum instead of restarting the argument.",
    "Connect every support point back to the topic's central claim.",
    "Use one concrete metric, mechanism, or example before claiming impact.",
]

ENEMY_SKILL_FRAGMENTS = [
    "Challenge the party's premise directly and keep pressure on their weakest claim.",
    "After rebutting, add a new concrete cost, failure mode, or counterexample.",
    "Rotate objections across evidence gaps, causal leaps, weak examples, tradeoffs, and burden of proof.",
    "Do not repeat the same objection twice; escalate to a different failure mode.",
]

PARTY_SUPPORT_SENTENCE = (
    "That matters because the strongest side connects its claim to clear evidence."
)
ENEMY_SUPPORT_SENTENCE = (
    "That matters because the burden of proof belongs to the side making the claim."
)

_GLUED_WORDS = frozenset(
    """
    a about accuracy accurate across actual actually add added adding against alien aliens all already
    an anecdotal and answer answered answers any appeal argument arguments are assertion at be because
    been being better burden but by can cannot case cases causal claim claims clear concrete cost costs
    could counter counterexample defying demand depends did do does due duties duty earth evidence
    example examples explain explained explaining explains extraordinary fact facts fail fails failure false
    from gap gaps had has have however
    if in into is it lack lacks law laws likely logic made manmade may mechanism mechanisms
    misidentification more must natural need needs no not objects of on only or phenomena physics
    point points premise proof prove proves rebut rebuttal rebuttals reason reasons recording recordings
    sightings side still
    strong stronger strongest support supports technology than that the their them there therefore these
    they this those to tradeoff tradeoffs true truth ufo ufos visited visits was weak where which while
    who why will with without would
    """.split()
)
_GLUED_WORD_MAX_LEN = max(len(part) for part in _GLUED_WORDS)

PERSONA_PROMPT_KEYS = (
    "archetype",
    "voice",
    "tagline",
    "bio",
    "backstory",
    "tone",
    "quirks",
    "views",
    "quotes",
    "domain_keywords",
    "focus",
    "evolution_notes",
)

HARNESS_TEXT_LIMIT = 900
BATTLE_REACTION_STATES = (
    "takes_damage",
    "deals_damage",
    "enemy_low_hp",
    "user_low_hp",
)
BATTLE_REACTION_LINE_MIN = 2
BATTLE_REACTION_LINE_MAX = 3


def normalize_persona(raw: Any, *, fallback_name: str = "") -> dict[str, Any]:
    """Return a compact persona dict that preserves legacy and hydrated fields."""
    persona = dict(raw or {}) if isinstance(raw, dict) else {}
    if raw and not isinstance(raw, dict):
        persona["voice"] = str(raw)
    if fallback_name and not persona.get("name"):
        persona["name"] = fallback_name
    if persona.get("bio") and not persona.get("backstory"):
        persona["backstory"] = persona["bio"]
    if persona.get("backstory") and not persona.get("bio"):
        persona["bio"] = persona["backstory"]
    if not persona.get("voice"):
        persona["voice"] = persona.get("tagline") or persona.get("bio") or persona.get("backstory")
    if persona.get("voice") and not persona.get("battle_voice"):
        persona["battle_voice"] = persona["voice"]

    for key in ("views", "quotes", "domain_keywords", "evolution_notes"):
        persona[key] = _string_list(persona.get(key))[:8]
    return persona


def normalize_harness(raw: Any, *, role: str = "party") -> dict[str, Any]:
    """Return a harness dict with canonical prompt and directive fields."""
    harness = dict(raw or {}) if isinstance(raw, dict) else {}
    if raw and not isinstance(raw, dict):
        harness["system_prompt"] = str(raw)
    if harness.get("system") and not harness.get("system_prompt"):
        harness["system_prompt"] = harness["system"]

    base_directives = _string_list(harness.get("directives"))
    role_directives = ENEMY_DIRECTIVES if role == "enemy" else PARTY_DIRECTIVES
    directives: list[str] = []
    for directive in [*base_directives, *role_directives, *BATTLE_RESPONSE_DIRECTIVES]:
        if directive not in directives:
            directives.append(directive)
    harness["directives"] = directives
    skill_fragments = _string_list(harness.get("skill_prompt_fragments"))
    role_fragments = ENEMY_SKILL_FRAGMENTS if role == "enemy" else PARTY_SKILL_FRAGMENTS
    for fragment in role_fragments:
        if fragment not in skill_fragments:
            skill_fragments.append(fragment)
    harness["skill_prompt_fragments"] = skill_fragments
    return harness


def build_battle_reactions(
    persona: dict[str, Any] | None,
    debate_type: Any = None,
    *,
    role: str = "party",
) -> dict[str, list[str]]:
    """Build a deterministic personality reaction bank for battle state changes."""
    p = normalize_persona(persona or {})
    style = _reaction_style(p)
    domain = _reaction_domain(debate_type)
    role = "enemy" if role == "enemy" else "party"
    templates = _enemy_reaction_templates(style, domain) if role == "enemy" else _party_reaction_templates(style, domain)
    return {
        state: [_clean_reaction_line(line) for line in templates[state]][:BATTLE_REACTION_LINE_MAX]
        for state in BATTLE_REACTION_STATES
    }


def ensure_battle_reactions(
    raw: Any,
    debate_type: Any = None,
    *,
    role: str = "party",
    fallback_name: str = "",
) -> dict[str, Any]:
    """Return normalized persona JSON with 2-3 battle reaction lines per state."""
    persona = normalize_persona(raw, fallback_name=fallback_name)
    generated = build_battle_reactions(persona, debate_type, role=role)
    existing = persona.get("battle_reactions")
    existing_map = existing if isinstance(existing, dict) else {}
    reactions: dict[str, list[str]] = {}
    for state in BATTLE_REACTION_STATES:
        lines: list[str] = []
        for raw_line in _string_list(existing_map.get(state)):
            line = _clean_reaction_line(raw_line)
            if line:
                lines.append(line)
            if len(lines) >= BATTLE_REACTION_LINE_MAX:
                break
        for line in generated[state]:
            if len(lines) >= BATTLE_REACTION_LINE_MIN:
                break
            if line and line not in lines:
                lines.append(line)
        reactions[state] = lines[:BATTLE_REACTION_LINE_MAX]
    persona["battle_reactions"] = reactions
    return persona


def select_battle_reaction(
    raw: Any,
    state: str,
    *,
    debate_type: Any = None,
    role: str = "party",
    seed: str | int = "",
) -> str:
    """Pick one deterministic reaction line from a persona's battle bank."""
    persona = ensure_battle_reactions(raw, debate_type, role=role)
    lines = persona.get("battle_reactions", {}).get(state, [])
    if not lines:
        return ""
    key = f"{persona.get('name','')}:{state}:{seed}".encode()
    idx = sum(key) % len(lines)
    return str(lines[idx])


def persona_prompt_line(persona: dict[str, Any]) -> str:
    """Render supported persona fields into a concise prompt fragment."""
    bits: list[str] = []
    labels = {
        "archetype": "archetype",
        "voice": "voice",
        "tagline": "tagline",
        "bio": "bio",
        "backstory": "backstory",
        "tone": "tone",
        "quirks": "quirks",
        "views": "views",
        "quotes": "quotes",
        "domain_keywords": "domains",
        "focus": "focus",
        "evolution_notes": "evolution",
    }
    for key in PERSONA_PROMPT_KEYS:
        rendered = _prompt_value(persona.get(key))
        if rendered:
            bits.append(f"{labels[key]}: {rendered}")
    return "; ".join(bits)


def harness_prompt_line(harness: dict[str, Any]) -> str:
    """Render trained harness fields, bounded so combat prompts stay fast."""
    parts: list[str] = []
    base = str(harness.get("system_prompt") or "").strip()
    if base:
        parts.append(f"system: {base}")
    directives = _string_list(harness.get("directives"))
    if directives:
        parts.append("directives: " + " | ".join(directives[:8]))
    fragments = _string_list(harness.get("skill_prompt_fragments"))
    if fragments:
        parts.append("techniques: " + " | ".join(fragments[:6]))
    return _truncate(" ".join(parts), HARNESS_TEXT_LIMIT)


def sanitize_battle_utterance(
    text: str,
    *,
    max_sentences: int = 2,
    max_words: int = 45,
    max_sentence_words: int = 22,
) -> str:
    """Normalize model output into the compact battle-utterance contract."""
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    text = re.sub(r"(\d+)\.\s+(\d+)", r"\1.\2", text)
    text = re.sub(r"[*_`#>]+", "", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\s*[-*•]\s*", "", line)
        line = re.sub(r"^\s*\d+[.)]\s*", "", line)
        if _looks_like_heading(line):
            continue
        line = _strip_label(line)
        if line:
            lines.append(line)

    compact = re.sub(r"\s+", " ", " ".join(lines) if lines else text.strip()).strip()
    compact = _repair_malformed_battle_format(compact)
    if not compact:
        return ""

    sentence_parts, trailing = _split_sentence_parts(compact)
    parts = sentence_parts + ([trailing] if trailing else [])
    if not parts:
        parts = [compact]

    cleaned: list[str] = []
    for part in parts:
        sentence = _strip_label(part.strip())
        sentence = _strip_procedural_prefix(sentence)
        if not sentence or _is_meta_sentence(sentence):
            continue
        cleaned.append(sentence)
        if len(cleaned) >= max_sentences:
            break

    if not cleaned:
        return ""
    return compress_battle_utterance(
        cleaned,
        max_sentences=max_sentences,
        max_words=max_words,
        max_sentence_words=max_sentence_words,
    )


def ensure_battle_sentence_floor(text: str, *, role: str = "party") -> str:
    """Ensure generated battle turns have at least two complete sentences."""
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if not compact:
        return ""
    sentences, trailing = _split_sentence_parts(compact)
    count = len([s for s in sentences if s.strip()]) + (1 if trailing else 0)
    if count >= 2:
        return compact
    if compact[-1] not in ".!?":
        compact += "."
    support = ENEMY_SUPPORT_SENTENCE if role == "enemy" else PARTY_SUPPORT_SENTENCE
    return f"{compact} {support}"


def compress_battle_utterance(
    sentences: list[str] | str,
    *,
    max_sentences: int = 2,
    max_words: int = 45,
    max_sentence_words: int = 22,
) -> str:
    """Low-latency local hook that keeps battle turns short without another LLM call."""
    if isinstance(sentences, str):
        raw_parts, trailing = _split_sentence_parts(sentences)
        parts = raw_parts + ([trailing] if trailing else [])
    else:
        parts = sentences

    selected: list[str] = []
    word_count = 0
    for sentence in parts:
        sentence = sentence.strip()
        if not sentence:
            continue
        words = sentence.split()
        if len(words) > max_sentence_words:
            if selected:
                continue
            if len(words) <= max_words:
                return sentence
            return _truncate_sentence(sentence, max_words)
        if selected and word_count + len(words) > max_words:
            break
        if not selected and len(words) > max_words:
            return _truncate_sentence(sentence, max_words)
        selected.append(sentence)
        word_count += len(words)
        if len(selected) >= max_sentences:
            break
    if selected:
        return " ".join(selected).strip()
    first = next((p.strip() for p in parts if p.strip()), "")
    if not first:
        return ""
    words = first.split()
    if len(words) <= max_words:
        return first
    return _truncate_sentence(first, max_words)


def _party_reaction_templates(style: str, domain: str) -> dict[str, list[str]]:
    return {
        "takes_damage": [
            f"{style}, that hit shows the objection has force, not that our case fails.",
            f"{style}, I absorb the pressure and tighten the link between claim and evidence.",
            f"{style}, pressure asks for cleaner proof, so I sharpen the strongest reason.",
        ],
        "deals_damage": [
            f"{style}, that landed because our {domain} point answered the live objection.",
            f"{style}, the enemy felt the hit where their objection skipped the central evidence.",
            f"{style}, damage follows evidence; the better mechanism still favors our side.",
        ],
        "enemy_low_hp": [
            f"{style}, their case is low because each objection retreats from the evidence.",
            f"{style}, press now: the enemy still has not answered the strongest {domain} proof.",
            f"{style}, they are running out of ground because the mechanism keeps surviving contact.",
        ],
        "user_low_hp": [
            f"{style}, we are low, so narrow the claim and defend the strongest mechanism.",
            f"{style}, low HP is not lost ground; it is a demand for cleaner evidence.",
            f"{style}, hold the line: our team argument still lives if we prove the link plainly.",
        ],
    }


def _enemy_reaction_templates(style: str, domain: str) -> dict[str, list[str]]:
    return {
        "takes_damage": [
            f"{style}, that hit stings but only proves the party found a narrow exception.",
            f"{style}, I concede the pressure, not the premise; the proof burden still stands.",
            f"{style}, pain clarifies the weak spot, so I move the argument back to evidence.",
        ],
        "deals_damage": [
            f"{style}, that landed because their claim still outran its evidence.",
            f"{style}, the damage follows the {domain} gap they still have not answered.",
            f"{style}, their case buckled where assertion had to become proof.",
        ],
        "enemy_low_hp": [
            f"{style}, I am low, but one precise counterexample can still collapse their premise.",
            f"{style}, low HP only sharpens the point: their strongest claim still dodges the cost.",
            f"{style}, pressure is useful because it exposes which proof burden they cannot carry.",
        ],
        "user_low_hp": [
            f"{style}, the party is low because conviction is not the same as evidence.",
            f"{style}, now press the gap: their case keeps borrowing certainty from anecdotes.",
            f"{style}, their HP falls where the {domain} argument fails to protect the proof burden.",
        ],
    }


def _reaction_style(persona: dict[str, Any]) -> str:
    tone = str(persona.get("tone") or "").strip().lower()
    quirk = _prompt_value(persona.get("quirks")).lower()
    voice = _prompt_value(persona.get("voice")).lower()
    tone_label = {
        "assertive": "Assertive and direct",
        "sardonic": "Dryly sardonic",
        "earnest": "Earnest and clear",
        "combative": "Combative and precise",
        "measured": "Measured and calm",
        "whimsical": "Whimsical but pointed",
        "relentless": "Relentless and focused",
    }.get(tone, "Focused and in character")
    if "question" in quirk or "journalist" in voice:
        return f"{tone_label} with a probing edge"
    if "data" in voice or "scientist" in voice or "papers" in quirk:
        return f"{tone_label} with evidence-first force"
    if "lawyer" in voice or "latin" in quirk:
        return f"{tone_label} with formal pressure"
    if "activist" in voice or "story" in voice:
        return f"{tone_label} with human stakes"
    if "popular culture" in quirk or "sports" in quirk:
        return f"{tone_label} with punchy framing"
    return tone_label


def _reaction_domain(debate_type: Any) -> str:
    value = str(getattr(debate_type, "value", debate_type) or "").strip().lower()
    return {
        "logos": "logic",
        "pathos": "human-stakes",
        "ethos": "credibility",
        "chaos": "reframe",
        "socratic": "questioning",
        "rhetoric": "framing",
    }.get(value, "argument")


def _clean_reaction_line(text: Any) -> str:
    line = sanitize_battle_utterance(
        str(text or ""),
        max_sentences=1,
        max_words=32,
        max_sentence_words=32,
    )
    if line and line[-1] not in ".!?":
        line += "."
    return line


def _prompt_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items() if str(v).strip())
    return str(value).strip() if value is not None else ""


def _strip_label(text: str) -> str:
    return re.sub(
        r"^(claim|support|evidence|rebuttal|counter|argument|for|against)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _repair_malformed_battle_format(text: str) -> str:
    text = re.sub(
        r"\b(claim|support|evidence|rebuttal|counter|argument)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([.!?])(?=[A-Z])", r"\1 ", text)
    text = re.sub(r"([a-z])([A-Z]{2,})", r"\1 \2", text)
    text = re.sub(r"\b([A-Z]{2,})([a-z]{3,})", r"\1 \2", text)
    text = re.sub(r"\b[A-Za-z]{16,}\b", _repair_glued_word_match, text)
    return re.sub(r"\s+", " ", text).strip()


def _repair_glued_word_match(match: re.Match[str]) -> str:
    word = match.group(0)
    parts = _segment_glued_word(word)
    if parts is None:
        return word
    if word[0].isupper():
        parts[0] = parts[0].capitalize()
    return " ".join(parts)


def _segment_glued_word(word: str) -> list[str] | None:
    lower = word.lower()
    if lower in _GLUED_WORDS:
        return None
    best: list[list[str] | None] = [None] * (len(lower) + 1)
    best[0] = []
    for start in range(len(lower)):
        current = best[start]
        if current is None:
            continue
        for end in range(start + 1, min(len(lower), start + _GLUED_WORD_MAX_LEN) + 1):
            piece = lower[start:end]
            if piece not in _GLUED_WORDS:
                continue
            candidate = [*current, piece]
            existing = best[end]
            if existing is None or len(candidate) < len(existing):
                best[end] = candidate
    parts = best[-1]
    if parts is None or len(parts) < 2:
        return None
    return parts


def _split_sentence_parts(text: str) -> tuple[list[str], str]:
    safe = re.sub(r"(\d)\.(\d)", r"\1<DECIMAL>\2", text)
    safe_parts = re.findall(r"[^.!?]+[.!?]", safe)
    consumed = "".join(safe_parts)
    trailing = safe[len(consumed) :].strip().replace("<DECIMAL>", ".")
    parts = [part.replace("<DECIMAL>", ".") for part in safe_parts]
    return parts, trailing


def _strip_procedural_prefix(text: str) -> str:
    text = re.sub(
        r"^i\s+(?:am\s+|'m\s+)?arguing\s+(for|against)\s+the\s+proposition\s+that\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text


def _is_meta_sentence(text: str) -> bool:
    lower = re.sub(r"\s+", " ", text.lower()).strip()
    meta_phrases = (
        "the user wants",
        "user wants me",
        "i need to",
        "i should",
        "i will now",
        "i am supposed to",
        "i'm supposed to",
        "my task is",
        "the prompt asks",
        "prompt wants",
        "as an ai",
        "my assigned stance",
        "assigned side",
        "exactly two plain sentences",
        "first answering",
        "second press",
        "debate as the",
        "instructions",
        "there is no opposing claim",
        "no opposing claim",
        "no opponent has spoken",
        "since i'm speaking first",
        "since i am speaking first",
        "claim that needs to be addressed",
        "needs to be addressed",
    )
    return any(phrase in lower for phrase in meta_phrases)


def _truncate_sentence(sentence: str, max_words: int) -> str:
    words = sentence.split()
    out = " ".join(words[:max_words]).rstrip(" ,;:")
    if out and out[-1] not in ".!?":
        out += "."
    return out


def _looks_like_heading(line: str) -> bool:
    if len(line) > 90:
        return False
    lowered = line.lower().strip(":")
    if lowered.startswith(("against ", "for ")):
        return not re.search(r"[.!?]$", line)
    return lowered in {"argument", "claim", "support", "evidence", "rebuttal"}


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
