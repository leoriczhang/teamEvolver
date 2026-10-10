#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m compileall teamEvolver team_skills team_memory team_miner team_replay session_ingestion team_ontology tests

if ! python - <<'PY' >/dev/null 2>&1
import pytest  # noqa: F401
PY
then
  echo "[verify] pytest is required; install the dev dependencies first" >&2
  exit 1
fi
python -m pytest

if ! command -v npm >/dev/null 2>&1; then
  echo "[verify] npm is required" >&2
  exit 1
fi
if [ ! -d web-ui/node_modules ]; then
  echo "[verify] web-ui dependencies are missing; run npm --prefix web-ui ci" >&2
  exit 1
fi

mkdir -p .verify-home .npm-cache .npm-logs
HOME="$ROOT_DIR/.verify-home" \
  npm_config_cache="$ROOT_DIR/.npm-cache" \
  npm_config_logs_dir="$ROOT_DIR/.npm-logs" \
  npm --prefix web-ui test
HOME="$ROOT_DIR/.verify-home" \
  npm_config_cache="$ROOT_DIR/.npm-cache" \
  npm_config_logs_dir="$ROOT_DIR/.npm-logs" \
  npm --prefix web-ui run build
