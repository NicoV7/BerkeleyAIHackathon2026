#!/usr/bin/env bash
# Pull the local models the gateway defaults to. Run after `docker compose up`.
# Bottom-up capability approach: small local models first, swap up later.
set -euo pipefail

SERVICE="${OLLAMA_SERVICE:-ollama}"
COMPOSE="docker compose -f $(dirname "$0")/../docker-compose.yml"

models=(
  "gemma3:4b"          # default debater / party
  "qwen3:4b"           # alternate debater
  "nomic-embed-text"   # embeddings for hybrid RAG
)

echo "Pulling models into the '$SERVICE' container..."
for m in "${models[@]}"; do
  echo "==> ollama pull $m"
  $COMPOSE exec -T "$SERVICE" ollama pull "$m"
done
echo "Done. Installed:"
$COMPOSE exec -T "$SERVICE" ollama list
