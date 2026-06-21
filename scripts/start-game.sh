#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
OPEN_BROWSER=1
PULL_MODELS=1

usage() {
  cat <<'EOF'
Usage: pnpm game:start [--no-open] [--skip-model-pull]

Starts the full local Debate RPG stack with Docker Compose, ensures the default
Ollama models are present, waits for the API health check, and opens the game.

Options:
  --no-open          Do not open the game in a browser.
  --skip-model-pull  Skip the Ollama model pull step.
  -h, --help         Show this help message.
EOF
}

log() {
  printf '\n==> %s\n' "$1"
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --)
        ;;
      --no-open)
        OPEN_BROWSER=0
        ;;
      --skip-model-pull)
        PULL_MODELS=0
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown option: $1"
        ;;
    esac
    shift
  done
}

prepare_env() {
  if [ -f "$ENV_FILE" ]; then
    return
  fi

  [ -f "$ENV_EXAMPLE" ] || fail ".env.example is missing."
  log "Creating .env from .env.example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
}

env_value_or_default() {
  local key="$1"
  local fallback="$2"
  local line value
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%%#*}"
  value="$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"

  if [ -z "$value" ]; then
    printf '%s' "$fallback"
  else
    printf '%s' "$value"
  fi
}

ensure_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker Desktop is required: https://www.docker.com/products/docker-desktop/"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required. Update Docker Desktop and rerun this script."
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker Desktop and rerun this script."
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local attempt

  for attempt in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$label" "$url"
      return
    fi
    sleep 2
  done

  fail "$label did not become ready at $url. Run 'pnpm logs' for details."
}

open_game() {
  local url="$1"
  if [ "$OPEN_BROWSER" -eq 0 ]; then
    printf 'Game URL: %s\n' "$url"
    return
  fi

  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  else
    printf 'Game URL: %s\n' "$url"
  fi
}

main() {
  local api_port web_port api_url game_url
  parse_args "$@"
  cd "$ROOT_DIR"
  prepare_env
  ensure_docker

  log "Starting Debate RPG"
  docker compose -f "$COMPOSE_FILE" up -d --build

  if [ "$PULL_MODELS" -eq 1 ]; then
    bash "$ROOT_DIR/infra/ollama/pull-models.sh"
  fi

  api_port="$(env_value_or_default API_PORT 8000)"
  web_port="$(env_value_or_default WEB_PORT 5173)"
  api_url="http://localhost:${api_port}/api/health"
  game_url="http://localhost:${web_port}"

  wait_for_http "$api_url" "API"
  open_game "$game_url"

  log "Game is running"
  printf 'API docs: http://localhost:%s/docs\n' "$api_port"
  printf 'Logs:     pnpm logs\n'
}

main "$@"
