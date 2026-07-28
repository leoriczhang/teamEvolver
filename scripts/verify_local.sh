#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m compileall teamEvolver tests

if python - <<'PY' >/dev/null 2>&1
import pytest  # noqa: F401
PY
then
  if python - <<'PY' >/dev/null 2>&1
import sqlite3  # noqa: F401
PY
  then
    python -m pytest
  else
    echo "[verify] Python sqlite3 module is unavailable; skipped sqlite-dependent Hermes capture test"
    mapfile -t TEST_FILES < <(find tests -name 'test_*.py' ! -name 'test_hermes_session_capture.py' | sort)
    python -m pytest "${TEST_FILES[@]}"
  fi
else
  echo "[verify] pytest is not installed; skipped Python tests"
fi

if command -v npm >/dev/null 2>&1 && [ -d web-ui/node_modules ]; then
  mkdir -p .verify-home .npm-cache .npm-logs
  HOME="$ROOT_DIR/.verify-home" \
    npm_config_cache="$ROOT_DIR/.npm-cache" \
    npm_config_logs_dir="$ROOT_DIR/.npm-logs" \
    npm --prefix web-ui run build
else
  echo "[verify] npm or web-ui/node_modules not available; skipped frontend build"
fi
