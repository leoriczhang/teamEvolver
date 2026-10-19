#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERIFY_WORK="$(mktemp -d "${TMPDIR:-/tmp}/te-verify.XXXXXX")"
trap 'rm -rf "$VERIFY_WORK"' EXIT
export PYTHONPYCACHEPREFIX="$VERIFY_WORK/pycache"

python -m compileall teamEvolver team_skills team_memory team_miner team_replay session_ingestion tests

if ! python - <<'PY' >/dev/null 2>&1
import pytest  # noqa: F401
PY
then
  echo "[verify] pytest is required; install the dev dependencies first" >&2
  exit 1
fi
python -m pytest -o "cache_dir=$VERIFY_WORK/pytest-cache"

if ! command -v npm >/dev/null 2>&1; then
  echo "[verify] npm is required" >&2
  exit 1
fi
if [ ! -d web-ui/node_modules ]; then
  echo "[verify] web-ui dependencies are missing; run npm --prefix web-ui ci" >&2
  exit 1
fi

mkdir -p "$VERIFY_WORK/home" "$VERIFY_WORK/npm-cache" "$VERIFY_WORK/npm-logs"
HOME="$VERIFY_WORK/home" \
  npm_config_cache="$VERIFY_WORK/npm-cache" \
  npm_config_logs_dir="$VERIFY_WORK/npm-logs" \
  npm --prefix web-ui test
HOME="$VERIFY_WORK/home" \
  npm_config_cache="$VERIFY_WORK/npm-cache" \
  npm_config_logs_dir="$VERIFY_WORK/npm-logs" \
  npm --prefix web-ui run build
