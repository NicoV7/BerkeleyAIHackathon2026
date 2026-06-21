#!/usr/bin/env bash
# Pull the local models the gateway defaults to. Run after `docker compose up`.
# Bottom-up capability approach: small local models first, swap up later.
set -euo pipefail

SERVICE="${OLLAMA_SERVICE:-ollama}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"

models=(
  "gemma3:1b"          # default low-latency local debater / judge
  "gemma3:4b"          # larger local debater / party option
  "qwen3:4b"           # alternate debater
  "nomic-embed-text"   # embeddings for hybrid RAG
)

echo "Pulling models into the '$SERVICE' container..."
for m in "${models[@]}"; do
  echo "==> ollama pull $m"
  docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE" ollama pull "$m"
done
echo "Done. Installed:"
docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE" ollama list
