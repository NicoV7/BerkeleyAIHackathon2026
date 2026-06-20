# WS-G — Human-Argues Pivot + Pixel Design System + Dual Reasoning Trend

## Goal
Pivot the game to **human-argues**: in a debate the player types an argument each round and
the LLM judge scores the *player*; the player's party monster is their avatar (its HP is the
player's HP, the typed argument is that monster's turn). Enemy agents still rebut
autonomously via the existing orchestrator. Land the dual demo "money shot": the player's
reasoning score climbing across rounds shown beside a trained agent's score jumping after a
training cycle (human learning beside machine learning). Re-skin the whole web app in a
"tasteful pixel" retro-JRPG identity.

Preserves: judge (`judge.py`), damage (`damage.py`), enemy generation/orchestration, the WS
event stream (utterance/verdict/hp/phase), the combatant/HP model, capture, gambits,
training. Changes: source of the party's turn text (human-typed, not gambit-generated) + UI.

## You OWN
Backend (additive only — coordinate, do not rewrite the engine):
- `apps/api/app/schemas.py` — **append** `PlayerArgueRequest {text, skill_id?}` (frozen file;
  additive model only).
- `apps/api/app/debate/orchestrator.py` — add `run_human_round_stream(...)` async generator
  (mirrors `run_round_stream` event protocol; reuses `_generate_utterance`, `score_round`,
  `compute_damage`, `_phase_for`, `append_utterance`, `set_hp`).
- `apps/api/app/routers/debate.py` — WS `argue` action branch + REST `POST /{eid}/argue`.

Frontend:
- `apps/web/index.html`, `apps/web/src/index.css` — pixel design system (fonts + palette vars
  + utility classes).
- `apps/web/src/ws/useEncounterStream.ts` — `hp`/`phase` handlers; expose `drive(rounds)` +
  `argue(text, skillId?)`; `capturableIds`.
- `apps/web/src/ui/BattleDebateView.tsx` — player argument bar (text + skill buttons),
  JRPG dialogue-box transcript with typewriter + verdict strike, segmented HP drain,
  capture/flee wiring, inviting empty state.
- `apps/web/src/ui/ReasoningTrend.tsx` (new) — hand-rolled pixel SVG dual line chart.
- `apps/web/src/lib/skills.ts` (new) — `parseSkill(s)` (string | object).
- `apps/web/src/ui/PartyScreen.tsx` — skill chips via `parseSkill`; remove gambit-link gate;
  fix invalid `int` aliases.
- `apps/web/src/ui/TrainingScreen.tsx` — `api.get<MonsterSummary[]>` party fetch; agent trend.
- `apps/web/src/ui/GambitEditor.tsx` — handle/remove the `target __custom__` option.
- `apps/web/src/ui/Overworld.tsx`, `apps/web/src/game/OverworldScene.ts` — Phaser pixelArt
  render + palette tints.
- `apps/web/src/App.tsx` — retro title card, topic chips, pixel nav.

## Backend contract additions (additive)
WS command on `/api/encounters/{eid}/stream` (alongside the existing `{"rounds": N}` drive):
```json
{ "action": "argue", "text": "<player argument>", "skill_id": "<optional skill name>" }
```
Server: record player utterance (actor_role `party`, actor_id = lead party monster) → emit
`utterance`; generate ONE enemy rebuttal → emit `utterance`; score both via `score_round`;
apply damage via `compute_damage` (player's `skill_mult` = the chosen skill's `power`, type =
the skill's type) → emit `verdict`(s) + `hp`; emit `phase`. REST fallback `POST
/{eid}/argue` (body `PlayerArgueRequest`) returns the existing `TurnResult`. `skill_id` is the
skill **name**; combatants rehydrated from Redis carry full `skills` objects so power/type
resolve server-side.

## Design system (tasteful pixel)
Fonts (Google): Press Start 2P (title + big numerals only), Silkscreen (HUD/labels/buttons),
JetBrains Mono (body/argument text). Palette vars in `index.css` per the spec
(`--bg/--panel/--panel2/--ink/--muted/--accent` gold/`--party` cyan/`--enemy` rose/
`--win/--warn/--danger`) + the six elemental type colors. `border-radius: 0` everywhere,
2–3px solid borders, `box-shadow: 4px 4px 0 #000`, buttons translate-on-press,
`image-rendering: pixelated` on canvas/img.

## Interfaces
- Expose (frontend): `useEncounterStream` → `{ ..., capturableIds, drive, argue }`;
  `parseSkill(s) -> {name, type, power, description}`; `<ReasoningTrend you? agent? />`.
- Expose (backend): `run_human_round_stream(eid, topic, combatants, run_id, start_turn,
  momentum, player_text, skill_id)`; `PlayerArgueRequest`.
- Consume: existing `score_round`, `compute_damage`, `_phase_for`, `_generate_utterance`,
  Redis helpers; `api`/`wsUrl` from `src/api/client.ts`; `useGame` store; training endpoints
  (`/api/monsters/{id}/train/gepa|grpo`, `/api/training/{job_id}/preference`).

## Definition of done
- WS `{"action":"argue","text":"..."}` → player utterance → player verdict → enemy rebuttal →
  verdict → hp → phase; encounter can reach won/lost/capturable.
- Battle runs end-to-end from the UI: type argument → judged, enemy rebuts, HP drains, reach
  win/capturable/lose live; Auto (3) still runs an autonomous debate.
- Player's reasoning line rises during battle; agent's line jumps after a training run with
  the `score_delta` shown as a big gold numeral; both series visible together.
- Skills render as chips (no raw JSON); GambitEditor reachable pre-battle; TrainingScreen
  party loads.
- `pnpm --filter web gen:types` picks up `PlayerArgueRequest`; `pnpm --filter web build`
  typechecks. No rounded corners / system-mono remain.

## Stretch (delivered)
- Overworld 16×16 procedural pixel sprites (`game/OverworldScene.ts`, baked via
  `Graphics.generateTexture`).
- Retro SFX via Tone.js (`lib/sfx.ts` + battle wiring; mute toggle). `tone` added to web deps.
- Design-system cleanup of the last un-restyled screens (`GambitEditor.tsx`,
  `TrainingScreen.tsx`).

## Rules
- Frozen shared files (`schemas.py`, `models.py`, `redis_state.py`, `main.py`,
  `packages/shared/enums.ts`): only the additive `PlayerArgueRequest` in `schemas.py`. Note
  it in `memories/ws-g-human-argue.md`.
- Do NOT edit `pyproject.toml`; charts are hand-rolled SVG (no recharts).
- When done: write `memories/ws-g-human-argue.md`, update `memories/MEMORY.md`, commit.
