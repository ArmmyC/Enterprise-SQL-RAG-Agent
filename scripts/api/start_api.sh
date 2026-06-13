#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export FAHMAI_API_HOST="${FAHMAI_API_HOST:-0.0.0.0}"
export FAHMAI_API_PORT="${FAHMAI_API_PORT:-8888}"

exec uvicorn api_server:app --host "$FAHMAI_API_HOST" --port "$FAHMAI_API_PORT"
