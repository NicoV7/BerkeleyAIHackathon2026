# Debate RPG — BerkeleyAIHackathon2026

A **Pokémon-style roguelike where you debate enemy AIs.** Walk a tile overworld,
collide into 1–3 enemy LLM agents arguing a topic you choose, and win by being
more persuasive. Your party are AI "monster" agents — you don't control them
directly; you author **Final Fantasy 12 "Gambit"** behavior rules and **train /
evolve** them (GEPA + GRPO human-in-the-loop) so they battle autonomously.
Capture defeated wild agents like Pokémon. Local-first, runs entirely on your machine.

## Stack
- **Frontend:** Vite + React + Phaser 3 (overworld) + Tailwind. (`apps/web`)
- **Backend:** Python FastAPI — agents, debate engine, RL training, hybrid RAG. (`apps/api`)
- **Models:** local-first gateway → Ollama (Gemma 3 / Qwen) with Claude/OpenAI pluggable.
- **Data:** Postgres + pgvector (memories/RAG), Redis (live encounter cache).

## Quickstart
```bash
cp .env.example .env
pnpm install                 # web deps (root workspace)
pnpm up                      # docker compose: postgres, redis, ollama, api, web
pnpm pull-models             # pull gemma3:4b, qwen3:4b, nomic-embed-text into ollama
open http://localhost:5173   # web   (API docs: http://localhost:8000/docs)
curl localhost:8000/api/health
```

## Hosted Model Fallbacks

The API gateway can route battle actors and judges through fast hosted providers
with a local Ollama fallback. Put provider keys only in the ignored root `.env`
file (`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `GEMINI_API_KEY`,
`OPENROUTER_API_KEY`); `.env.example` intentionally contains placeholders only.

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
