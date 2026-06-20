# WS-E — Party, Capture & Progression

## Goal
The "catch 'em like Pokémon" loop and progression: capture weakened wild enemies into
your party, award XP / level up / evolve party members, and the party management screen.

## You OWN
- `apps/api/app/party/capture.py` — `attempt_capture(session, encounter_id, wild_id) -> (success, monster)`: success probability scales with how low the wild's HP is (read from Redis hp map); on success flip `owner` to 'player', seed initial CHARACTER memory (call WS-D `write_event` if available, else skip).
- `apps/api/app/party/progress.py` — `award_xp(session, monster, amount)`, leveling curve, `maybe_evolve(monster)` (bump evolution_stage + max_hp at thresholds), skill unlocks.
- `apps/api/app/routers/capture.py` — `POST /api/encounters/{id}/capture` (CaptureRequest -> CaptureResult) and `GET /api/runs/{id}/party` (-> list[MonsterSummary]).
- `apps/web/src/ui/PartyScreen.tsx` — list party members with type/level/xp/hp/skills; show capture results; entry point to the Gambit editor (link, not embed — WS-C owns the editor).

## Interfaces
- Consume: models (Monster, MonsterOwner), schemas (CaptureRequest/Result, MonsterSummary), `app.redis_state.get_hp_map`, get_session; optionally `app.memory.store.write_event` (guard import).
- Expose: `attempt_capture`, `award_xp`, `maybe_evolve` — WS-B's encounter finalize calls `award_xp` on win (it will import if present; ship it). Capture is triggered by the frontend via your endpoint.

## Capture math (simple, tunable)
- `p = clamp(base + (1 - hp/max_hp) * scale, 0, 0.95)`; roll; on success add to party. A wild must be in the capturable window (hp < ~25%) to attempt (return success=False with a message otherwise).

## Progression
- XP from winning encounters; level thresholds (e.g. 100*level). On level up: +max_hp, maybe unlock a skill, evolve at stages (e.g. level 5/10).

## Definition of done
- pytest/script: set a wild's Redis HP low, `attempt_capture` flips owner with reasonable probability; `award_xp` levels a monster and `maybe_evolve` bumps stage.
- `GET /api/runs/{id}/party` returns members; `POST .../capture` works against the stack.
- `pnpm --filter web build` typechecks.

## Rules
- Router ownership: you own `capture.py` (capture + party listing). WS-C owns `party.py` (gambits). Do NOT both define the same route path. If you need party endpoints beyond listing, put them in `capture.py` or a new `progression.py` router (it auto-mounts only if named in main.py OPTIONAL_ROUTERS — add the name to your memory file for the orchestrator to wire, OR name your file one of the existing optional names you own).
- Do NOT edit frozen shared files (models, redis_state, schemas, enums, main.py). Add Optional schema fields only if essential; note in memory file.
- Do NOT edit pyproject.toml; note new deps in `memories/ws-e-party.md`.
- When done: write `memories/ws-e-party.md` and commit.
