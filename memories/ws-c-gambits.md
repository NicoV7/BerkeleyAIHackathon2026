# WS-C — Gambits + Battle/Debate UI

## Owns
- `apps/api/app/debate/gambits.py` — FF12 gambit rule engine: `choose_action(monster, battle_state) -> dict`
- `apps/api/app/routers/party.py` — `GET/PUT /api/monsters/{id}/gambits` + `GET /api/monsters/{id}`
- `apps/web/src/ui/BattleDebateView.tsx` — live encounter screen (export: `BattleDebateView`)
- `apps/web/src/ui/GambitEditor.tsx` — gambit CRUD editor (export: `GambitEditor`)
- `apps/web/src/ws/useEncounterStream.ts` — WebSocket hook (export: `useEncounterStream`)
- `apps/web/src/vite-env.d.ts` — added this missing Wave-0 file so `import.meta.env` typechecks

## Decisions

### Gambit engine
- `choose_action(monster, battle_state)` walks `monster._gambits` (list of `GambitRule`-like objects) sorted by ascending `priority`.
- First enabled rule whose condition evaluates true returns its action dict.
- Falls back to `{"kind": "default"}` if no match or empty gambit list.
- Import-safe: catches all exceptions, never raises at import time.
- `battle_state` keys consumed: `hp`, `max_hp`, `last_verdict_score`, `turn_no`, `topic`, `momentum`, `self_id`, `ally_ids`, `enemy_ids`.

### Router
- Party router only owns gambit routes + `GET /api/monsters/{id}`.
- WS-E must use `capture.py` / `party_progress.py` for capture/party-listing.
- Full-replace PUT: deletes all existing GambitRules then inserts new list.

### Frontend
- `useEncounterStream` connects to `WS /api/encounters/{id}/stream`, handles `utterance`, `verdict`, `state` message types.
  Auto-reconnects with 2s delay (stops on component unmount or `disconnect()` call).
- `BattleDebateView` reads `activeEncounterId` from zustand `useGame` store.
  Buttons: Next Round (POST /turn), Auto 3 (POST /auto {rounds:3}), Capture (POST /capture when phase=capturable), Flee (clears activeEncounterId locally).
- `GambitEditor` takes `monsterId` prop; no default export dependency on store.

## Interfaces

### WS-B must pass to choose_action
```python
battle_state = {
    "hp":                dict[str, int],   # monster_id -> current HP
    "max_hp":            dict[str, int],   # monster_id -> max HP
    "last_verdict_score": float,           # last judge verdict score
    "turn_no":           int,              # 0-indexed turn
    "topic":             str,              # debate topic
    "momentum":          dict[str, float], # "party"/"enemy" -> float
    "self_id":           str,              # this monster's id
    "ally_ids":          list[str],        # same-side monster ids (excluding self)
    "enemy_ids":         list[str],        # opposing monster ids
}
```

Import: `from app.debate.gambits import choose_action`

### WS-B WebSocket server must emit (for stream hook)
```json
{"type": "utterance", "data": {...Utterance fields...}}
{"type": "verdict",   "data": {...JudgeVerdict fields...}}
{"type": "state",     "data": {...EncounterState fields...}}
```

### App.tsx (WS-orchestrator, Wave 2)
```tsx
import BattleDebateView from "./ui/BattleDebateView";
import GambitEditor from "./ui/GambitEditor";
// wire BattleDebateView at screen === "encounter"
// wire GambitEditor at screen === "party" with a selected monsterId
```

## New deps (no pyproject changes needed)
- None; all imports from existing frozen modules (sqlmodel, fastapi, pydantic).
- Frontend: no new npm deps beyond what Wave-0 installed.

## Open TODOs (Wave 2)
- App.tsx: replace PlaceholderPanel for "encounter" with `<BattleDebateView />`.
- App.tsx: replace PlaceholderPanel for "party" with `<GambitEditor monsterId={...} />` (needs selected monster id from party list WS-E exposes).
- BattleDebateView: Flee should POST /api/encounters/{id}/flee once WS-B exposes that endpoint (currently just clears store locally).
- useEncounterStream: stop auto-reconnect once `phase` is "won"/"lost" (requires tracking phase in hook state — currently reconnects indefinitely).
- GambitEditor: skill_id input should be a dropdown populated from GET /api/skills once WS-A seeds skills.
- Party router `GET /api/monsters/{id}` could conflict if WS-E defines the same path in a different router; coordinate with WS-E on ownership.
