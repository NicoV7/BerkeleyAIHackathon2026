#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
OPEN_LINKS=1

usage() {
  cat <<'EOF'
Usage: pnpm install:game [--no-open]

Installs workspace dependencies, prepares the local .env file, pulls/builds
Docker runtime dependencies, and opens API-key pages for missing optional hosted
model providers.

Options:
  --no-open   Print API-key links without opening browser tabs.
  -h, --help  Show this help message.
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
        OPEN_LINKS=0
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

ensure_pnpm() {
  if command -v pnpm >/dev/null 2>&1; then
    return
  fi

  if command -v corepack >/dev/null 2>&1; then
    log "Activating pnpm with Corepack"
    corepack enable
    corepack prepare pnpm@10.28.0 --activate
  fi

  command -v pnpm >/dev/null 2>&1 || fail "pnpm is required. Install Node.js 20+ with Corepack, then rerun this script."
}

ensure_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker Desktop is required: https://www.docker.com/products/docker-desktop/"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required. Update Docker Desktop and rerun this script."
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker Desktop and rerun this script."
}

prepare_env() {
  if [ -f "$ENV_FILE" ]; then
    log "Keeping existing .env"
    return
  fi

  [ -f "$ENV_EXAMPLE" ] || fail ".env.example is missing."
  log "Creating .env from .env.example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
}

env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%%#*}"
  value="$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

needs_key() {
  local value
  value="$(env_value "$1")"
  case "$value" in
    "" | changeme | CHANGE_ME | your_* | *_here | placeholder)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

open_url() {
  local url="$1"
  if [ "$OPEN_LINKS" -eq 0 ]; then
    return
  fi

  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  fi
}

open_key_links() {
  local specs spec name rest key url opened
  specs=(
    "Anthropic|ANTHROPIC_API_KEY|https://console.anthropic.com/settings/keys"
    "OpenAI|OPENAI_API_KEY|https://platform.openai.com/api-keys"
    "Groq|GROQ_API_KEY|https://console.groq.com/keys"
    "Cerebras|CEREBRAS_API_KEY|https://cloud.cerebras.ai/platform/"
    "Gemini|GEMINI_API_KEY|https://aistudio.google.com/app/apikey"
    "OpenRouter|OPENROUTER_API_KEY|https://openrouter.ai/settings/keys"
  )
  opened=0

  log "API-key pages for optional hosted model providers"
  for spec in "${specs[@]}"; do
    name="${spec%%|*}"
    rest="${spec#*|}"
    key="${rest%%|*}"
    url="${rest#*|}"

    if needs_key "$key"; then
      printf '  %s (%s): %s\n' "$name" "$key" "$url"
      open_url "$url"
      opened=1
    fi
  done

  if [ "$opened" -eq 0 ]; then
    printf '  All optional provider key slots already have values in .env.\n'
  elif [ "$OPEN_LINKS" -eq 0 ]; then
    printf '  Browser opening disabled; copy any links you need.\n'
  fi
}

install_dependencies() {
  log "Installing pnpm workspace dependencies"
  pnpm install --frozen-lockfile

  log "Pulling Docker service images"
  docker compose -f "$COMPOSE_FILE" pull postgres redis ollama

  log "Building game containers"
  docker compose -f "$COMPOSE_FILE" build api web
}

main() {
  parse_args "$@"
  cd "$ROOT_DIR"
  ensure_pnpm
  ensure_docker
  prepare_env
  install_dependencies
  open_key_links

  log "Install complete"
  printf 'Run the game with: pnpm game:start\n'
}

main "$@"
