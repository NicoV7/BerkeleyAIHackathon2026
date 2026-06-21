---
name: Rhetorical Flourish
type: RHETORIC
power: 1.3
domain: RHETORIC
mp_cost: 40
special: preempt_rebut
effect_kind: agent_argument
target: enemy
duration_turns: 0
requires_prompt: false
rarity: common
modifiers: damage_mult=1.0
---

# Rhetorical Flourish

**Move type:** RHETORIC (style / framing — COUNTER move)
**Cost:** 40 MP — a premium move. Spend it to seize the exchange before the
opponent even speaks.

## What it does
A counter-skill: you do not just answer the room — you answer the move your
opponent is *about* to make. The engine reads the enemy's most recent line
straight off the shared Redis transcript (`enc:{encounter_id}:transcript`) — on
round one, before any rebuttal exists, it uses the enemy's already-materialized
**opening** as the predicted line. That line is injected into your turn as
"their likely next move," and you pre-empt it: name the rebuttal coming for you,
dismantle it in advance, and land a memorable flourish so the audience hears
your answer before they ever hear the objection.

Crucially this costs **zero extra model calls**: the enemy line is text that
already exists in Redis (or the cached opening), so reading it is a lookup, not
a generation. Your single turn is still your single turn — it is just primed
with the opponent's predicted move.

## How to deploy it this turn
- Take the enemy's predicted line you were handed and state, in your own words,
  the rebuttal they will reach for.
- Pre-empt it: explain why that move fails *before* they can make it.
- Compress the win into one rhythmic, balanced sentence — a rule of three, a
  contrast, a callback — and land it last so it is the line that echoes.

## Example
"They'll tell you the costs are speculative — but speculation doesn't bankrupt
families, policy does: we were promised a ladder and handed a treadmill, and
no rebuttal yet has explained who pays for the climb."
