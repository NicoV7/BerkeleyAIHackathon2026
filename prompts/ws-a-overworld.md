# WS-A — Overworld (Pokémon-style map + encounter trigger)

## Goal
A walkable tile overworld where the player moves grid-by-grid, wild enemies wander,
and colliding with one triggers a debate encounter. Plus the `/api/runs` + `/api/map`
+ `/api/move` endpoints and the starter-party roll.

## You OWN (only touch these)
- `apps/web/src/game/**` — Phaser scene(s): `OverworldScene.ts`, `EncounterTrigger.ts`, `tilemap/`
- `apps/web/src/ui/Overworld.tsx` — React wrapper that mounts the Phaser canvas
- `apps/web/public/assets/**` — any sprite/tileset assets (procedural/simple colored tiles are fine)
- `apps/api/app/routers/map.py` — exposes `router` (FastAPI APIRouter)
- `apps/api/app/party/generator.py` — `roll_starter_party(run_id)` and wild-enemy generation

## Endpoints to implement (shapes are FROZEN in `app/schemas.py`)
- `POST /api/runs` (CreateRunRequest -> RunState): create a Run row, roll a 2-3 member starter party (persona/type/skills/harness), return RunState with party.
- `GET /api/runs/{id}/map` (-> MapState): deterministic-from-seed grid (e.g. 20x15), 0=walkable/1=blocked, place player + a few wandering wild enemies (Monster rows with owner='wild').
- `POST /api/runs/{id}/move` (MoveRequest -> MoveResult): update player_x/y if walkable; if the new tile collides with a wild enemy, create nothing yet — just return that enemy's id as `encounter_id` field is set by WS-B's create; for now return `encounter_id=None` but include collision info. SIMPLEST: on collision, call a thin hook `maybe_start_encounter(run_id, wild_id)` that you stub to return a placeholder id; WS-B owns real encounter creation. Coordinate via the interface note below.

## Interfaces
- Consume: `from app.gateway.gateway import gateway` (for persona flavor text generation — keep it optional/fast), `get_session` from `app.db.session`, models from `app.db.models`, schemas from `app.schemas`.
- Expose: `roll_starter_party(session, run_id) -> list[Monster]` and `generate_wild(session, run_id, n) -> list[Monster]` in `party/generator.py` — WS-B and WS-E import these.
- For encounter creation on collision, DO NOT implement the debate; return the wild monster id and let the frontend call `POST /api/encounters` (WS-B). Your MoveResult.encounter_id may stay None; put the collided wild id in a new optional field you add to MoveResult ONLY IF NEEDED (prefer: frontend reads enemies from MapState and calls encounters itself).

## Persona/type/skills generation (shared concept — keep simple, deterministic-ish)
- Random persona = `{backstory, tone, quirks}`; random `DebateType`; 2-3 skills (you may seed a tiny inline skill list since the global Skill catalog is seeded in Wave 2); harness = `{system_prompt}` derived from persona+type.

## Frontend
- Phaser 3 (already a web dependency). Mount in `Overworld.tsx`. Arrow/WASD movement, collision with wild sprites. On collision, set the active encounter via the zustand store (`useGame().setEncounter(...)`) — for now you can navigate to the encounter screen; WS-B/WS-C render the battle.
- Keep Phaser confined to the overworld canvas. Simple colored-rectangle tiles/sprites are acceptable (no art dependency).

## Definition of done
- `pnpm --filter web build` typechecks.
- The three endpoints work against the running stack (api auto-reloads from the main tree — you can unit-test by importing your router/functions in the api venv at `apps/api/.venv`).
- Walking the map in the browser moves the player and detects collisions.

## Rules
- Do NOT edit shared frozen files: `db/models.py`, `redis_state.py`, `schemas.py`, `packages/shared/enums.ts`, `main.py` (it auto-mounts your `map` router). If you truly need a schema field, add it as Optional and note it in your memory file.
- Do NOT edit pyproject.toml. If you need a new dependency, note it in `memories/ws-a-overworld.md` for the orchestrator.
- When done: write `memories/ws-a-overworld.md` (owns / decisions / interfaces / open TODOs) and `git add -A && git commit`.
