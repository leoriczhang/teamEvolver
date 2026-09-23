#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export HOME="${DATA_ROOT:-$ROOT/runtime/customer}"
mkdir -p "$HOME"
export TEAMEVOLVER_CONFIG_FILE="${TEAMEVOLVER_CONFIG_FILE:-$HOME/config.yaml}"
export TEAMEVOLVER_CONFIG_BOOTSTRAP=1
export TEAMEVOLVER_SKILLMINER_ENABLED="${TEAMEVOLVER_SKILLMINER_ENABLED:-1}"
: "${TEAMEVOLVER_ROOT_API_KEY:?Set a random root key of at least 32 characters}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
exec "$PYTHON" -m uvicorn teamEvolver.customer:create_app \
  --factory --host "${HOST:-0.0.0.0}" --port "${PORT:-52010}" \
  --workers 1 --limit-concurrency "${TEAMEVOLVER_HTTP_CONCURRENCY:-256}"
