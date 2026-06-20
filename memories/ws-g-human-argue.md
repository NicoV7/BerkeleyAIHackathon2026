# WS-G — Human-Argues Pivot + Pixel Design System + Dual Reasoning Trend

## Owns
Backend (additive):
- `app/schemas.py` — `PlayerArgueRequest {text, skill_id?}` (only frozen-file touch; additive).
- `app/debate/orchestrator.py` — `run_human_round_stream(...)` + helpers `_lead`,
  `_resolve_skill`, `_json_dumps`.
- `app/routers/debate.py` — WS `{"action":"argue"}` branch + REST `POST /{eid}/argue` +
  `_run_one_human_round`.

Frontend:
- `apps/web/index.html` (Google Fonts), `src/index.css` (full pixel design system).
- `src/lib/skills.ts` (new) — `parseSkill`/`parseSkills`/`typeColor`/`TYPE_COLOR`.
- `src/ui/ReasoningTrend.tsx` (new) — hand-rolled pixel SVG dual line chart.
- `src/ws/useEncounterStream.ts` — hp/phase handlers, `capturableIds`, `drive`, `argue`.
- `src/ui/BattleDebateView.tsx` — full rewrite: player argument bar + skill buttons,
  typewriter, verdict strike, segmented HP drain, capture/flee, live "You" trend.
- `src/ui/TrainingScreen.tsx`, `src/ui/PartyScreen.tsx`, `src/ui/GambitEditor.tsx`,
  `src/ui/Overworld.tsx`, `src/game/OverworldScene.ts`, `src/App.tsx`, `src/state/store.ts`.
- `prompts/ws-g-human-argue-pixel.md` (brief).

## Decisions (why)
- **Human-argues is a separate round generator, not a modified `run_round_stream`.**
  `run_human_round_stream` reuses `_generate_utterance`/`score_round`/`compute_damage`/
  `_phase_for` but applies damage inline (not via `_apply_round_damage`) because the player's
  chosen skill must scale `skill_mult` per-actor — the shared helper hardcodes 1.0.
- **`skill_id` == skill name.** Skills have no separate id; combatants rehydrated from Redis
  carry full `skills` objects, so `_resolve_skill` looks up type+power by name server-side.
- **WS branch keys on `msg.action == "argue"`** vs the existing `{"rounds": N}` drive — both
  paths coexist; the autonomous loop is untouched (Auto/Next Round still use it).
- **Charts hand-rolled SVG**, no `recharts` (not installed; pixel aesthetic is sharper with
  crispEdges segments and no new dep). SFX/Tone.js left as out-of-scope stretch.
- **Dual "money shot"**: player per-round scores published to the zustand store
  (`lastYouScores`) by BattleDebateView so TrainingScreen can render the human curve beside
  the agent curve. Agent curve = baseline 60 then cumulative `score_delta` per GEPA/GRPO cycle.
- **Design system**: global `border-radius: 0 !important` + `.pixel-panel/.pixel-btn/
  .pixel-field` + hard `4px 4px 0 #000` shadows; fonts Press Start 2P (display) / Silkscreen
  (HUD) / JetBrains Mono (body). Elemental type colors promoted to CSS vars + `typeColor()`.

## Interfaces
- Backend: `run_human_round_stream(eid, topic, combatants, run_id, start_turn, momentum,
  player_text, skill_id)` → yields `Event(utterance|verdict|hp|phase)`; `PlayerArgueRequest`;
  `POST /api/encounters/{eid}/argue` → `TurnResult`; WS `{"action":"argue","text","skill_id"}`.
- Frontend: `useEncounterStream(eid)` → `{..., capturableIds, drive(rounds), argue(text,skillId?)}`;
  `parseSkill(s)->{id,name,type,power,description}`; `<ReasoningTrend series=[{label,color,points}] />`.

## Open TODOs / notes
- **Combatant model dependency**: `app/party/generator.py` hardcodes `model="gemma3:1b"`
  (lines 133, 172). Enemy/party LLM utterances are EMPTY (fall back to "(X presses the
  point…)") unless `ollama pull gemma3:1b` is run — only `gemma3:4b` + `nomic-embed-text`
  were pulled initially. Judge uses `gemma3:4b` so verdicts/scores are always real. Pulled
  `gemma3:1b` to fix; no code change. (Same issue affects auto mode — pre-existing.)
- Local dev runs Postgres+Redis via `infra/docker-compose.yml` (postgres/redis only) + native
  Ollama; API via uvicorn with `DATABASE_URL`/`REDIS_URL`/`OLLAMA_BASE_URL` → localhost.
- `pnpm --filter web gen:types` re-run after the schema add — `PlayerArgueRequest` is in
  `packages/shared/types.gen.ts`. `pnpm --filter web build` typechecks clean.

## Stretch additions (post-core, parallel subagents)
- **Overworld pixel sprites** (`game/OverworldScene.ts`): player/enemy rects replaced with
  procedurally-baked 16×16 textures via `Graphics.generateTexture` (no asset files) — cyan
  knight + rose horned blob. Pulsing tween now scales relative to the baked display scale.
- **Retro SFX** (`lib/sfx.ts` new + `BattleDebateView.tsx`): Tone.js wrapper, lazy
  `Tone.start()` on first gesture, `enabled` flag + `setSfxEnabled`, all calls try/caught so
  audio never throws into React. `sfxBlip` (keypress), `sfxSubmit`, `sfxHit(positive)` on new
  verdict, `sfxCapture`, `sfxWin/Lose` on phase end. Mute toggle (🔊/🔇) in the action bar.
  Added `tone` to web deps.
- **Design-system cleanup**: `GambitEditor.tsx` + `TrainingScreen.tsx` (GEPA/GRPO sections)
  were the last un-restyled screens — converted to `.pixel-*` classes + CSS vars, removed all
  `font-mono` / hardcoded indigo-emerald-sky / `text-red/green-400`. Whole web app now passes
  a design-system audit (no rounded/system-mono/raw-semantic-tailwind left).

Verified: `pnpm --filter web exec tsc -b` clean; `pnpm --filter web build` succeeds.

Related: [[ws-b-debate]] (engine/judge/damage reused), [[ws-c-gambits]] (battle UI replaced),
[[ws-e-party]] (party/capture), [[ws-f-training]] (trend consumes score_delta).
