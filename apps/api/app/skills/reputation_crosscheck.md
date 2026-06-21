---
name: Reputation Crosscheck
type: ETHOS
power: 1.4
domain: ETHOS
mp_cost: 40
effect_kind: agent_argument
target: enemy
duration_turns: 0
requires_prompt: false
rarity: rare
modifiers: damage_mult=1.08
special: redis_memory_attack
---

# Reputation Crosscheck

**Move type:** ETHOS (memory / credibility)

## What it does
Crosscheck the opponent's current claim against what the Redis transcript shows
they already said, then attack the credibility gap.

## How to deploy it this turn
- Identify the remembered statement that sets their standard.
- Show how their current line fails that standard.
- End by making trust the issue: why should anyone follow that speaker?
