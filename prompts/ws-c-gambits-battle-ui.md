# WS-C — Gambits + Battle/Debate UI

## Goal
The Final Fantasy 12 "Gambit" system: a condition->action rule engine that drives
party members' autonomous battle behavior, an editor UI to author the rules, and the
live battle/debate view that streams the encounter over WebSocket.

## You OWN
- `apps/api/app/debate/gambits.py` — the condition DSL + evaluator: `choose_action(monster, battle_state) -> action dict`
- `apps/api/app/routers/party.py` — gambit CRUD ONLY: `GET/PUT /api/monsters/{id}/gambits` (GambitList). (Other party endpoints belong to WS-E; coordinate — you own this router file, expose only gambit routes here; WS-E will use a different router file `capture.py`/`party_progress.py`. If overlap, keep party.py to gambits + a GET /api/monsters/{id}.)
- `apps/web/src/ui/GambitEditor.tsx` — author/reorder/enable rules
- `apps/web/src/ui/BattleDebateView.tsx` — the encounter screen: combatants + HP bars + streaming transcript + judge verdicts + "auto" / "next round" / capture buttons
- `apps/web/src/ws/useEncounterStream.ts` — WebSocket hook consuming `WS /api/encounters/{id}/stream`

## Gambit DSL (keep small + composable)
- Condition: `{kind, op, value}` where kind ∈ {self_hp_pct, ally_hp_pct, enemy_hp_pct, last_verdict_score, turn_no, topic_keyword, momentum}; op ∈ {<,<=,>,>=,==,contains}.
- Action: `{kind, ...}` where kind ∈ {use_skill (skill_id), target (who: lowest_hp_enemy/specific), tone (value), default}.
- `choose_action`: walk enabled rules by ascending `priority`, first matching condition wins; else `{kind:'default'}`.
- Evaluate against a `battle_state` dict the orchestrator passes (hp map, last verdict, turn_no, topic, momentum). Define the expected `battle_state` keys in a docstring so WS-B can match.

## Interfaces
- Expose: `choose_action(monster, battle_state)` — WS-B imports this (it falls back to default if your module is absent, so ship it).
- Consume: schemas (GambitRuleModel, GambitList), models (GambitRule, Monster), get_session; `api`/`wsUrl` from `apps/web/src/api/client.ts`; `useGame` store.

## Frontend
- BattleDebateView reads the active encounter id from the store, opens the WS stream, renders a chat-style transcript with per-side coloring, HP bars, and the judge's running verdicts. Buttons: "Next round" (POST /turn), "Auto" (POST /auto), "Capture" (when capturable — calls WS-E's endpoint), "Flee".
- GambitEditor: list rules for a monster, add/remove/reorder (priority), toggle enabled, PUT to save.

## Definition of done
- `choose_action` unit-testable with a fake battle_state (add a tiny pytest).
- `pnpm --filter web build` typechecks.
- Battle view renders and streams against a live encounter (depends on WS-B; if WS-B isn't merged yet, mock the WS messages to prove the UI).

## Rules
- Do NOT edit frozen shared files (models, redis_state, schemas, enums, main.py) — your `party` router auto-mounts. Add Optional schema fields only if essential; note in memory file.
- Do NOT edit pyproject.toml; note new deps in `memories/ws-c-gambits.md`.
- Coordinate the `party.py` router ownership: you own gambit routes; WS-E owns capture/progression in its OWN router file. Do not both define the same path.
- When done: write `memories/ws-c-gambits.md` and commit.
