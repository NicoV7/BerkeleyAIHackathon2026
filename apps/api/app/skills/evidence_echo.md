---
name: Evidence Echo
type: LOGOS
power: 1.35
domain: LOGOS
mp_cost: 35
effect_kind: agent_argument
target: enemy
duration_turns: 0
requires_prompt: false
rarity: rare
modifiers: damage_mult=1.05
special: redis_memory_attack
---

# Evidence Echo

**Move type:** LOGOS (memory / proof)

## What it does
Echo a remembered opponent claim from the Redis transcript and answer it with a
specific fact, causal link, or testable standard in the agent's own voice.

## How to deploy it this turn
- Start from the exact remembered point you are answering.
- Add one concrete piece of proof or reasoning.
- End by showing why that proof defeats the remembered point.
