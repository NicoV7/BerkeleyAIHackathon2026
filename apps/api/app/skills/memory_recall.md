---
name: Memory Recall
type: SOCRATIC
power: 1.6
domain: GENERAL
mp_cost: 60
special: redis_peek
---

# Memory Recall

**Move type:** SOCRATIC (questioning + callback)
**Cost:** 60 MP — the most expensive move in the catalog. Use it once per battle.

## What it does
Your monster peeks the shared encounter transcript cached in Redis at
`enc:{encounter_id}:transcript`, picks the enemy line that just hit the player
hardest, quotes it back word-for-word, and materializes a one-sentence counter
in your monster's own persona voice (`persona.voice` or `persona.tagline`).
The cache is literally on screen: the player sees the Redis key, the live
transcript slice, and the highlighted line glow as their monster answers it.

Because Memory Recall is an *ability* and not a judged turn, the engine applies
a deterministic baseline (`score=80`) through the standard `compute_damage`
formula — so the type chart, ATK/DEF stat ratio, level scaling, and
`domain_match_mult` all still apply. On a cache miss (no transcript yet, Redis
down) the move never 500s: the counter degrades to a generic line, half the MP
cost is refunded, and damage is reported as 0.
