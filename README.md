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
