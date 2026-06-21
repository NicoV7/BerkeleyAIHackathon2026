---
name: battle-skill-effect-testing
description: Use when changing debate battle skills, skill frontmatter metadata, MP gates, Redis one-turn effects, judge modifiers, opponent output limits, gacha skill assignment, or the battle skill UI.
---

# Battle Skill Effect Testing

## Core Checks

When changing battle skills, cover the smallest layer that owns the behavior:

- Catalog parsing: assert every skill has `effect_kind`, `mp_cost`, `target`, `duration_turns`, `requires_prompt`, `rarity`, and `modifiers`.
- Gacha assignment: assert generated and pulled agents receive exactly two unique skills biased to their debate type.
- MP gates: assert unaffordable skills do not spend MP, advance the round, or emit misleading effects.
- One-turn effects: assert Redis effects are emitted, applied once, and cleared after the round.
- Judge sway: assert score deltas are visible in verdict rationale and clamped to 0-100.
- Status/output limits: assert enemy prompt contract and token budget change only for the affected turn.
- UI state: assert skill chips show effect kind/cost/duration and preview skills use the non-advancing WS action.

## Test Shape

Prefer pure unit tests for catalog parsing, generation, score/damage modifiers, and reducers. Use integration tests only for Redis/WebSocket lifecycle behavior. Keep tests Arrange-Act-Assert, deterministic under seeded RNG, and independent of external LLM calls.
