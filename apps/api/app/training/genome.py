"""Monster genome read/write + mutation operators (WS-F).

The "genome" is the prompt/behavior DNA of a debater — the ONLY thing training
optimizes (never model weights). It is assembled from a Monster's JSONB columns:

    {
        "harness":  {... orchestration / scoring knobs, incl. system_prompt ...},
        "persona":  {... name, tone, voice, backstory ...},
        "skill_prompt_fragments": [ "...", "..." ],   # injected debate moves
        "gambit_rules": [ {priority, condition, action}, ... ],  # FF12 behavior
    }

`read_genome(monster)`  -> dict (deep copy, safe to mutate)
`mutate(genome, op)`    -> new genome with one operator applied
`apply_genome(session, monster, genome, *, kind, score_delta, accepted, before)`
                        -> persists genome onto the Monster row, bumps
                           genome_version, writes a TrainingArtifact.

Operators are pure dict transforms so GEPA/GRPO can sample them without I/O.
"""
from __future__ import annotations

import copy
import random
from typing import Any, Optional

# All mutation operator names — the bandit / GEPA samplers draw from this list.
OPERATORS: list[str] = [
    "tweak_system_prompt",
    "shift_tone",
    "add_skill_fragment",
    "sharpen_skill_fragment",
    "reprioritize_gambits",
    "tighten_persona",
]

# Tone adjectives the shift_tone operator can swap in.
_TONES = [
    "incisive and confident",
    "calm and methodical",
    "aggressive and relentless",
    "witty and disarming",
    "warm but firm",
    "coldly precise",
    "theatrical and bold",
]

# Reusable prompt-fragment phrases the add/sharpen operators can introduce.
_FRAGMENTS = [
    "Open by conceding one small point to lower your opponent's guard, then pivot hard.",
    "Anchor every claim to a concrete example or number the audience can picture.",
    "Name the strongest version of the opposing view, then dismantle exactly that.",
    "End each turn with a single sharp question that reframes the debate on your terms.",
    "Use a vivid analogy to make an abstract point land emotionally.",
    "Cite a credible authority or precedent to borrow its weight.",
    "Expose the hidden assumption in your opponent's last claim before rebutting it.",
]

# System-prompt directives appended/swapped by tweak_system_prompt.
_DIRECTIVES = [
    "Stay ruthlessly on-topic; never grant ground you cannot reclaim.",
    "Win the framing first — define the terms before arguing the substance.",
    "Be concise: one crisp claim, one support, one rebuttal per turn.",
    "Read the judge's last verdict and adapt to what scored well.",
    "Match your opponent's strongest move with an even stronger counter of a different type.",
]


# ---------------------------------------------------------------- read / build


def read_genome(monster: Any) -> dict[str, Any]:
    """Extract a deep-copied genome dict from a Monster row (or any object/dict
    exposing harness/persona/skills)."""
    harness = _get(monster, "harness") or {}
    persona = _get(monster, "persona") or {}
    skills = _get(monster, "skills") or []

    # skill_prompt_fragments live inside harness (we never mutate the Monster's
    # `skills` catalog list directly — that is shared/seeded data).
    fragments = list(harness.get("skill_prompt_fragments", []))
    gambits = list(harness.get("gambit_rules", []))

    return copy.deepcopy(
        {
            "harness": {k: v for k, v in harness.items()
                        if k not in ("skill_prompt_fragments", "gambit_rules")},
            "persona": persona,
            "skill_prompt_fragments": fragments,
            "gambit_rules": gambits,
            "skills": list(skills),
        }
    )


def _get(obj: Any, attr: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def system_prompt(genome: dict[str, Any]) -> str:
    """Assemble the full system prompt a debater agent runs with, from genome."""
    persona = genome.get("persona", {})
    harness = genome.get("harness", {})
    name = persona.get("name", "a debater")
    dtype = persona.get("type") or harness.get("type") or "LOGOS"
    tone = persona.get("tone", "incisive and confident")
    backstory = persona.get("backstory", "")
    base = harness.get(
        "system_prompt",
        f"You are {name}, a competitive debater of the {dtype} school.",
    )
    parts = [base, f"Your debating tone is {tone}."]
    if backstory:
        parts.append(backstory)
    for d in harness.get("directives", []):
        parts.append(d)
    fragments = genome.get("skill_prompt_fragments", [])
    if fragments:
        parts.append("Techniques you favor:")
        parts.extend(f"- {f}" for f in fragments)
    return "\n".join(parts)


# ------------------------------------------------------------------ mutation


def mutate(
    genome: dict[str, Any],
    op: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> tuple[dict[str, Any], str]:
    """Return (new_genome, op_used). One operator applied; input untouched."""
    rng = rng or random
    op = op or rng.choice(OPERATORS)
    g = copy.deepcopy(genome)
    h = g.setdefault("harness", {})
    p = g.setdefault("persona", {})

    if op == "tweak_system_prompt":
        directive = rng.choice(_DIRECTIVES)
        dirs = h.setdefault("directives", [])
        if directive not in dirs:
            dirs.append(directive)
        else:
            # already have it — rewrite the base instead
            h["system_prompt"] = (h.get("system_prompt", "") + " " + directive).strip()

    elif op == "shift_tone":
        cur = p.get("tone")
        choices = [t for t in _TONES if t != cur]
        p["tone"] = rng.choice(choices) if choices else rng.choice(_TONES)

    elif op == "add_skill_fragment":
        frags = g.setdefault("skill_prompt_fragments", [])
        choices = [f for f in _FRAGMENTS if f not in frags]
        if choices:
            frags.append(rng.choice(choices))

    elif op == "sharpen_skill_fragment":
        frags = g.setdefault("skill_prompt_fragments", [])
        if frags:
            i = rng.randrange(len(frags))
            frags[i] = frags[i].rstrip(".") + " — and press the advantage immediately."
        else:
            frags.append(rng.choice(_FRAGMENTS))

    elif op == "reprioritize_gambits":
        rules = g.setdefault("gambit_rules", [])
        if len(rules) >= 2:
            rng.shuffle(rules)
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    r["priority"] = i
        # if 0/1 rules, op is a no-op (still a valid variant)

    elif op == "tighten_persona":
        p["focus"] = rng.choice(
            ["aggression", "precision", "framing", "empathy", "credibility"]
        )

    else:
        raise ValueError(f"Unknown mutation op: {op}")

    return g, op


def sample_mutations(
    genome: dict[str, Any],
    k: int,
    rng: Optional[random.Random] = None,
    weights: Optional[dict[str, float]] = None,
) -> list[tuple[dict[str, Any], str]]:
    """Draw k distinct-ish mutated variants. `weights` biases op selection
    (bandit). Returns list of (variant_genome, op)."""
    rng = rng or random
    out: list[tuple[dict[str, Any], str]] = []
    ops_pool = OPERATORS
    if weights:
        pop = list(weights.keys()) or OPERATORS
        wts = [max(weights.get(o, 1.0), 0.01) for o in pop]
        chosen = rng.choices(pop, weights=wts, k=k)
    else:
        chosen = [rng.choice(ops_pool) for _ in range(k)]
    for op in chosen:
        out.append(mutate(genome, op, rng))
    return out


# --------------------------------------------------------------- persistence


def apply_genome(
    session: Any,
    monster: Any,
    genome: dict[str, Any],
    *,
    kind: str = "gepa",
    score_delta: float = 0.0,
    accepted: bool = True,
    before: Optional[dict[str, Any]] = None,
) -> Any:
    """Write the genome back onto the Monster row, bump genome_version, and
    record a TrainingArtifact. Returns the artifact (un-committed; caller commits).

    `before` is the genome snapshot prior to training (for the artifact); if not
    given we read it from the monster's current state.
    """
    from app.db.models import TrainingArtifact  # local import: avoid cycles

    genome_before = before if before is not None else read_genome(monster)

    # Fold fragments + gambits back into harness so they persist on the row.
    new_harness = dict(genome.get("harness", {}))
    new_harness["skill_prompt_fragments"] = list(genome.get("skill_prompt_fragments", []))
    new_harness["gambit_rules"] = list(genome.get("gambit_rules", []))

    if accepted:
        monster.harness = new_harness
        monster.persona = dict(genome.get("persona", {}))
        # `skills` is the seeded catalog — leave it unless the genome carries one.
        if genome.get("skills"):
            monster.skills = list(genome["skills"])
        monster.genome_version = int(getattr(monster, "genome_version", 1) or 1) + 1
        session.add(monster)

    artifact = TrainingArtifact(
        monster_id=monster.id,
        kind=kind,
        genome_before=genome_before,
        genome_after=genome,
        score_delta=float(score_delta),
        accepted=bool(accepted),
        # created_at comes from the model's naive-UTC default (_now).
    )
    session.add(artifact)
    return artifact
