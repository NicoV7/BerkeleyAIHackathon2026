# Debate RPG — BerkeleyAIHackathon2026

Debate RPG is a local-first creature-collector RPG where the monsters are
autonomous debate agents. Explore a tile overworld, meet villagers and rival
agents, descend into nearby dungeons, and trigger turn-based argument battles
against 1-3 enemy LLM personas. You do not micromanage every line your party
says: you collect agents, give them gambit-style behavior rules, train their
prompt genomes, and let them argue for you.

The core loop blends Pokémon-style capture, old-school RPG overworld traversal,
and AI debate tactics. Winning means making the stronger case, using agent
skills at the right moment, remembering prior encounters, and evolving your
party into faster, sharper rhetorical specialists. The stack runs locally with
Ollama by default, with optional hosted model keys for lower-latency fallback
providers.

## Stack
- **Frontend:** Vite + React + Phaser 3 (overworld) + Tailwind. (`apps/web`)
- **Backend:** Python FastAPI — agents, debate engine, RL training, hybrid RAG. (`apps/api`)
- **Models:** local-first gateway → Ollama (Gemma 3 / Qwen) with Claude/OpenAI pluggable.
- **Data:** Postgres + pgvector (memories/RAG), Redis (live encounter cache).

## Quickstart
```bash
pnpm install:game
pnpm game:start
```

`pnpm install:game` installs workspace dependencies, prepares `.env`, pulls and
builds Docker dependencies, and opens API-key pages for any missing optional
hosted model providers. The game still runs fully local with Ollama if you leave
those keys blank.

`pnpm game:start` starts Postgres, Redis, Ollama, the FastAPI server, and the
Vite/Phaser web client. It also pulls the default Ollama models and opens
<http://localhost:5173>. API docs are available at <http://localhost:8000/docs>.

Useful commands:

```bash
pnpm install:game --no-open       # install without opening API-key pages
pnpm game:start --no-open         # start services and print the game URL
pnpm game:start --skip-model-pull # start faster if models are already local
pnpm logs                         # follow Docker service logs
pnpm down                         # stop the local stack
```

## Hosted Model Fallbacks

The API gateway can route battle actors and judges through fast hosted providers
with a local Ollama fallback. Put provider keys only in the ignored root `.env`
file. Latency-first defaults can use `GROQ_API_KEY`, `CEREBRAS_API_KEY`,
`GEMINI_API_KEY`, and `OPENROUTER_API_KEY`; `ANTHROPIC_API_KEY` and
`OPENAI_API_KEY` are available for explicit provider routing. `.env.example`
intentionally contains placeholders only.

Default latency-first candidates are configured with:

```bash
GATEWAY_ACTOR_CANDIDATES=groq/llama-3.1-8b-instant,cerebras/llama-3.3-70b,gemini/gemini-2.5-flash-lite,openrouter/openrouter/free,ollama/gemma3:1b
GATEWAY_JUDGE_CANDIDATES=groq/llama-3.3-70b-versatile,cerebras/llama-3.3-70b,gemini/gemini-2.5-flash,ollama/gemma3:1b
```

Use the Pareto pseudo-models from code paths that should prefer the fastest
acceptable provider:

```python
await gateway.complete(messages, model="pareto-actor")
await gateway.complete(messages, model="pareto-judge", json_mode=True)
```

To refresh the in-process benchmark frontier, run from `apps/api`:

```bash
uv run python -m app.scripts.bench_models --role actor --runs 3
uv run python -m app.scripts.bench_models --role judge --runs 3
```

The redacted runtime status is available at `GET /api/models/pareto`.

## Battle Harness Training

Run a small prompt-genome loop that trains both the party agent and the enemy
agent against each other. The loop uses the Pareto actor model by default,
scores latency first, and keeps only mutations that clear quality/reliability
floors.

```bash
cd apps/api
uv run python -m app.scripts.run_battle_training --cycles 1 --rounds 1 --variants 1 --model pareto-actor
```

The runner prints JSON with party/enemy score deltas, accepted mutation ops,
latency measurements, and final genomes. It does not fine-tune weights and does
not print provider secrets.

Encounter pacing is controlled by `BATTLE_DAMAGE_MULTIPLIER` (default `1.0`).
Raise it to shorten battles, lower it if playtests feel too abrupt.

## Translations

Authored web copy (intro NPC dialogue, choice prompts, party/quest empty
states) is wrapped with [General Translation](https://generaltranslation.com)
`<T>` components and translated by their CDN. Locale config lives in
[`apps/web/gt.config.json`](apps/web/gt.config.json) (defaults to English
source + Spanish target). Put `GT_PROJECT_ID` / `VITE_GT_PROJECT_ID` and
`GT_API_KEY` in your root `.env` (placeholders in `.env.example`), then run:

```bash
pnpm --filter web gt:translate
```

Locale artifacts are gitignored, so a fresh clone needs one `gt:translate`
run before localized text appears in the locale dropdown. Dynamic LLM-generated
NPC responses and Python-side POI labels are not yet translated.

## Layout
```
apps/web/         Vite + React + Phaser
apps/api/         FastAPI (gateway, debate, memory, training, party, routers, db)
packages/shared/  shared TS enums + generated API types
infra/            docker-compose, postgres init, ollama model pull
prompts/          plan files + per-workstream subagent briefs
memories/         in-repo agent memory log (handoff between waves/sessions)
```

See [prompts/ok-claude-use-toasty-pascal.md](prompts/ok-claude-use-toasty-pascal.md) for the full build plan.
