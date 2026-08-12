#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
HERMES_BIN="${TEAMEVOLVER_HERMES_BIN:-$ROOT_DIR/.venv/bin/hermes}"
HERMES_HOME_DIR="${TEAMEVOLVER_HERMES_HOME:-$ROOT_DIR/teamEvolver/skillminer/.hermes_home}"
CONFIG_TEMPLATE="$ROOT_DIR/teamEvolver/skillminer/hermes/config.yaml.example"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Project Hermes is not installed at: $HERMES_BIN" >&2
  echo "Run: bash scripts/install_teamEvolver.sh" >&2
  exit 1
fi

mkdir -p "$HERMES_HOME_DIR"
if [[ ! -f "$HERMES_HOME_DIR/config.yaml" ]]; then
  cp "$CONFIG_TEMPLATE" "$HERMES_HOME_DIR/config.yaml"
  echo "Initialized project Hermes config: $HERMES_HOME_DIR/config.yaml" >&2
fi

export HERMES_HOME="$HERMES_HOME_DIR"
export PYTHONNOUSERSITE=1
unset HERMES_INFERENCE_MODEL HERMES_IGNORE_USER_CONFIG

exec "$HERMES_BIN" "$@"
