# WS-A — Overworld Memory (Wave 1)

## Owns
- `apps/api/app/routers/map.py` — `/api/runs`, `/api/runs/{id}/map`, `/api/runs/{id}/move`
- `apps/api/app/party/generator.py` — `roll_starter_party`, `generate_wild`
- `apps/web/src/game/OverworldScene.ts` — Phaser 3 tile scene
- `apps/web/src/game/EncounterTrigger.ts` — collision→encounter bridge
- `apps/web/src/game/tilemap/MapGenerator.ts` — client tile helpers
- `apps/web/src/ui/Overworld.tsx` — React wrapper mounting Phaser canvas

## Decisions

### datetime naive UTC workaround
`models.py` (`_now()`) returns `datetime.now(timezone.utc)` (aware), but the
postgres schema uses `TIMESTAMP WITHOUT TIME ZONE`. All three `Monster` and `Run`
creations in `map.py` / `generator.py` override `created_at=datetime.utcnow()`
(naive). This is a frozen-contract gap — the wave-0 `models.py` `_now()` default
produces a mismatch. Noted here so the orchestrator can fix `_now()` globally in
Wave 2 if desired (change to `datetime.utcnow()` or use `TIMESTAMP WITH TIME ZONE`
in the schema).

### Session API — use `session.execute()` not `session.exec()`
`get_session` yields a raw SQLAlchemy `AsyncSession` (not SQLModel's subclass).
Use `await session.execute(select(...))` + `.scalars().all()` for SELECT queries.
`session.get(Model, pk)`, `.add()`, `.commit()`, `.refresh()` work fine.

### Map generation
- 20×15 grid, deterministic from `run.seed` via `random.Random(seed)`.
- ~12% random wall density; edges and spawn zone (x≤2, y≤2) are always clear.
- Wild enemies placed with a second RNG seeded at `seed ^ 0xABCD`.
- `GET /map` is stateless and always re-derives tiles from seed — no caching needed.

### Collision detection
`POST /move` re-derives enemy positions from DB + seed on every call.
On collision, `encounter_id` in `MoveResult` is set to the wild Monster's id
(not a real Encounter id — WS-B creates the Encounter). The frontend
calls `POST /api/encounters` with `wild_id=encounter_id` to begin the battle.

### Wild enemy seeding
Wild enemies are created in DB at run-creation time (`POST /api/runs` calls
`generate_wild(n=5)`). They persist with `owner=wild` so `GET /map` can fetch
them consistently.

### Inline skill catalog
A tiny 12-skill list lives in `generator.py`. The global Skill catalog seeded
in Wave 2 will replace/extend this. WS-B/WS-C should prefer the DB `skills`
table when it exists; the JSON blob in `Monster.skills` is a fallback.

## Interfaces Exposed

### `roll_starter_party(session, run_id, seed=0) -> list[Monster]`
- File: `apps/api/app/party/generator.py`
- Creates 2–3 `owner=player` Monster rows with persona/harness/skills.
- Used by WS-B (loading party into encounter) and WS-E (party screen).

### `generate_wild(session, run_id, n=4, seed=0) -> list[Monster]`
- File: `apps/api/app/party/generator.py`
- Creates n `owner=wild` Monster rows.
- Used by WS-B (wild enemy in encounter) and WS-E (capture target).

### REST endpoints
| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/runs` | Create run + party; returns `RunState` |
| GET | `/api/runs/{id}/map` | Tile grid + player + wild positions; returns `MapState` |
| POST | `/api/runs/{id}/move` | Move player; returns `MoveResult` with optional `encounter_id` |

### Frontend: `Overworld` default export
- `apps/web/src/ui/Overworld.tsx`
- Reads `runId` from zustand store; auto-mounts Phaser canvas.
- On encounter collision: calls `POST /api/encounters` (WS-B) then `setEncounter()`.
- Fallback: if WS-B is absent, uses `wildId` as the encounter ref.

## Frozen-Contract Gaps / Notes for Orchestrator

1. **`datetime.utcnow()` deprecation warning** — Python 3.12 warns on `utcnow()`.
   Ideal fix: change `TIMESTAMP WITHOUT TIME ZONE` → `TIMESTAMP WITH TIME ZONE` in
   the schema (requires Alembic migration or recreating tables) OR change `_now()`
   in `models.py` to `datetime.utcnow()` (naive). Currently worked around per-call.

2. **`MoveResult.encounter_id` dual meaning** — per the brief, `encounter_id` is
   supposed to be a real Encounter row id, but WS-A returns the wild Monster id
   because WS-B creates the Encounter. The frontend's `EncounterTrigger.ts` handles
   this by posting to `/api/encounters` to get the real id before calling
   `setEncounter`. Wave 2 may want to atomically create the encounter in `/move`.

3. **Phaser canvas size** — hardcoded to `min(20*32, window.innerWidth-16)`.
   A responsive resize listener can be added in Wave 2.

## Open TODOs for Wave 2

- Wire `<Overworld />` into `App.tsx` (orchestrator task per brief).
- Add animated player sprite sheet (currently a solid blue rectangle).
- Add NPC / trainer enemy types (not just wild).
- Persist map-level state (wild enemy removal after capture) in Redis or DB.
- Add flee mechanic: player moves away from encounter resets `activeEncounterId`.
- Replace inline skill catalog with queries to the seeded `skills` DB table (WS-C/Wave 2).
