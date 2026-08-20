#!/usr/bin/env bash
# One-command setup for Tzara (macOS / Linux).
#   ./setup.sh
# First run: copies .env.template -> .env, generates a strong random
# POSTGRES_PASSWORD into it, then STOPS so you can review .env before anything
# is built. Run it again to build and start the stack (which also pulls the
# default local models on first start). An existing .env is never overwritten.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed or not on PATH. Install Docker Desktop / Docker Engine first." >&2
  exit 1
fi

# Strong random secret (48 hex chars: safe for .env, sed, and Postgres alike).
gen_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 48
  fi
}

if [ ! -f .env ]; then
  cp .env.template .env
  # Replace the placeholder BEFORE any container starts: Postgres bakes this
  # value into its data volume on first init and never re-reads it, so it must
  # be the final value the very first time `up` runs.
  pw="$(gen_password)"
  tmp="$(mktemp)"
  sed "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${pw}|" .env > "$tmp" && mv "$tmp" .env

  echo "Created .env from .env.template."
  echo "  -> A strong random POSTGRES_PASSWORD was generated for you."
  echo "  -> Review .env now (vaults location, models, GPU overlay, etc.),"
  echo "     then run ./setup.sh again to build and start Tzara."
  exit 0
fi

echo ".env already exists; using it."

# Read a KEY=value from .env (last assignment wins), trimming whitespace. Fallbacks
# below mirror the defaults in docker-compose.yml so the message matches what
# `ollama-init` actually pulls even when .env omits these.
env_val() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]'; }

# LLM_* replaced OLLAMA_* when Tzara stopped being Ollama-only; read the new name
# first and fall back to the old one, matching config.py and the compose defaults.
llm_env_val() { v="$(env_val "$1")"; [ -n "$v" ] || v="$(env_val "$2")"; printf '%s' "$v"; }

PORT="$(env_val PORT)";                        PORT="${PORT:-8000}"
CHAT_MODEL="$(llm_env_val LLM_MODEL OLLAMA_MODEL)"
CHAT_MODEL="${CHAT_MODEL:-llama3.2:3b}"
EMBED_MODEL="$(llm_env_val LLM_EMBED_MODEL OLLAMA_EMBED_MODEL)"
EMBED_MODEL="${EMBED_MODEL:-embeddinggemma:300m}"
COMPOSE_FILE_VAL="$(env_val COMPOSE_FILE)"
LLM_URL_VAL="$(llm_env_val LLM_URL OLLAMA_URL)"

# The external-inference overlay puts the local ollamaserver AND the ollama-init
# model-pull job behind an inactive Compose profile, so their images are never
# pulled and nothing is downloaded when it's active.
EXTERNAL_INFERENCE=0
case "$COMPOSE_FILE_VAL" in *external-inference*) EXTERNAL_INFERENCE=1 ;; esac

# Stop on the easy misconfiguration: LLM_URL aimed at an external server
# (anything other than the in-compose `ollama` host) WITHOUT that overlay would
# still build/start a local Ollama container and download models the user doesn't
# need. Refuse to build so they fix .env first - no wasted images or model pulls.
if [ "$EXTERNAL_INFERENCE" -eq 0 ]; then
  case "$LLM_URL_VAL" in
    ""|http://ollama:*|http://ollama|https://ollama:*|https://ollama) : ;;
    *)
      echo "ERROR: LLM_URL points at an external server ($LLM_URL_VAL), but the" >&2
      echo "external-inference overlay is not in COMPOSE_FILE. Building now would start a" >&2
      echo "LOCAL Ollama container and download models you don't need." >&2
      echo >&2
      echo "To use ONLY your external server, set this in .env, then re-run ./setup.sh:" >&2
      echo "  COMPOSE_FILE=docker-compose.yml:docker-compose.external-inference.yml" >&2
      echo "(':' is the macOS/Linux path separator; COMPOSE_PATH_SEPARATOR=: is the default.)" >&2
      exit 1
      ;;
  esac
fi

echo "Building and starting Tzara (docker compose up --build -d)..."
docker compose up --build -d

echo
echo "Tzara is starting."
if [ "$EXTERNAL_INFERENCE" -eq 1 ]; then
  echo "Using an external inference server (${LLM_URL_VAL:-see LLM_URL in .env}); the"
  echo "local Ollama container and model-pull step are disabled - no models are downloaded."
else
  echo "First run pulls the configured models (${CHAT_MODEL} + ${EMBED_MODEL}) via the"
  echo "ollama-init service - this can take several minutes on a fresh machine."
fi
echo
echo "  Readiness check:  curl -s http://localhost:${PORT}/health"
echo "  Open the wiki:    http://localhost:${PORT}/"
