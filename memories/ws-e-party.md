# WS-E — Party, Capture & Progression Memory

## Owns
- `apps/api/app/party/capture.py` — `attempt_capture(session, encounter_id, wild_id) -> (bool, Monster|None, str)`
- `apps/api/app/party/progress.py` — `award_xp(session, monster, amount) -> dict`, `maybe_evolve(monster) -> bool`
- `apps/api/app/routers/capture.py` — auto-mounted (name "capture" is in OPTIONAL_ROUTERS in main.py)
- `apps/web/src/ui/PartyScreen.tsx` — exported default component

## Decisions

### Capture
- Capturable window: `hp < 25% of max_hp` (CAPTURABLE_HP_FRACTION = 0.25)
- Probability: `p = clamp(0.15 + (1 - hp/max_hp) * 0.80, 0, 0.95)`
- At 10% HP: p ≈ 0.87; at 24% HP: p ≈ 0.76; at exactly 25% HP: rejected
- HP fallback: if Redis has no HP entry for monster, falls back to max_hp (= 100%) → rejected. This is safe: encounter must be in progress for capture to succeed.
- Memory seeding: guarded import of `app.memory.store`. Sets `monster._capture_memory_hint` on success for async write at router layer (not yet wired — Wave 2 TODO).

### Progression
- `xp_needed(level) = 100 * level` — linear curve
- Level-up: +10 max_hp per level
- Skill unlocks: at levels 3, 6, 9 — appends a tag string `"skill_L{N}"` to `monster.skills` JSONB list
- Evolution: stage 1 at level 5, stage 2 at level 10. +20 max_hp on each evolution.
- `award_xp` is synchronous (no await). Caller must `session.add(monster)` and commit.
- `maybe_evolve` is also synchronous, called inside the level-up loop.

### Routing
- `POST /api/encounters/{encounter_id}/capture` — CaptureRequest (wild_id) → CaptureResult
- `GET  /api/runs/{run_id}/party` — → list[MonsterSummary]
- Router file is named `capture.py`, listed in OPTIONAL_ROUTERS → auto-mounted by main.py.
- WS-C owns `party.py` (gambit routes). No path conflicts.

### Frontend
- `PartyScreen.tsx` exported as default; does NOT modify App.tsx (Wave 2 wire-up).
- Uses `useGame()` store for `runId` and `activeEncounterId`.
- "Edit Gambits" link uses `window.location.hash = gambits/{monster.id}` — WS-C's GambitEditor should listen for this hash route.
- `CaptureResult` state: PartyScreen can accept a `captureResult` prop or expose a method; currently it holds its own state via `setCaptureResult` (can be called from encounter UI in Wave 2).

## Interfaces

### Exported (for WS-B)
```python
from app.party.progress import award_xp
# award_xp(session, monster, amount) -> dict
# dict keys: levelled (bool), new_level (int), skills_unlocked (list[str]), evolved (bool)
# Synchronous. Does session.add(monster). Caller commits.

from app.party.capture import attempt_capture
# async attempt_capture(session, encounter_id, wild_id) -> (bool, Monster|None, str)
```

### Consumed
- `app.redis_state.get_hp_map(encounter_id)` → `dict[str, int]`
- `app.db.models.Monster, MonsterOwner`
- `app.schemas.CaptureRequest, CaptureResult, MonsterSummary`
- `app.db.session.get_session`
- `app.memory.store` — optional, guarded import

## Open TODOs (Wave 2)
1. Wire `PartyScreen` into `App.tsx` for the "party" screen (replace PlaceholderPanel).
2. Async memory write on capture: after `attempt_capture` returns success, call `await store.write_event(...)` with the `_capture_memory_hint` if WS-D's store is available.
3. Expose `setCaptureResult` or a store event so the encounter/battle UI (WS-B/WS-C) can push capture results directly into PartyScreen without a full navigation.
4. Gambit hash-route listener in WS-C's GambitEditor should handle `gambits/{monster_id}`.
5. Consider adding current HP to MonsterSummary (Optional[int]) — needs reading from Redis for active encounters. Note in schemas if added.
6. `award_xp` is import-safe for WS-B (guarded: `try: from app.party.progress import award_xp`).

## New Deps
None — all existing packages. No pyproject.toml changes.

## Schema Fields Added
None. All uses stay within the frozen schema. `MonsterSummary.skills: list[Any]` was already `list[Any]` — we populate it with strings.
