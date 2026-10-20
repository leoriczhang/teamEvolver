#!/usr/bin/env bash
# Developer install from this checkout: hashed locked dependencies, then the
# project in editable mode with the dev tools. Releases are installed from
# app.tar by cicd_scripts/install.sh instead.
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${TEAMEVOLVER_VENV_DIR:-$ROOT_DIR/.venv}"
APPLY_SCHEMA=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--venv-dir PATH] [--python BIN] [--apply-schema]

  --apply-schema  run 'teamEvolver schema apply' (needs TEAMEVOLVER_PG_DSN)

Then start a local all-in-one runtime with:
  teamEvolver start --role all
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv-dir) VENV_DIR="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --apply-schema) APPLY_SCHEMA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -U pip
"$VENV_DIR/bin/python" -m pip install --require-hashes -r requirements.lock
"$VENV_DIR/bin/python" -m pip install --no-deps -e .
"$VENV_DIR/bin/python" -m pip install ruff pytest pytest-asyncio anyio build
"$VENV_DIR/bin/python" -m pip check

if [[ "$APPLY_SCHEMA" -eq 1 ]]; then
  "$VENV_DIR/bin/teamEvolver" schema apply
fi
echo "[install_teamEvolver] ready: source \"$VENV_DIR/bin/activate\""
