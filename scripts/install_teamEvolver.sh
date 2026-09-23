#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${TEAMEVOLVER_VENV_DIR:-$ROOT_DIR/.venv}"
INSTALL_EXTRAS="${TEAMEVOLVER_INSTALL_EXTRAS:-all}"
RUN_SETUP=0
RUN_START=0
SKIP_HERMES="${TEAMEVOLVER_SKIP_HERMES:-0}"
HERMES_PACKAGE_SPEC="${TEAMEVOLVER_HERMES_PACKAGE_SPEC:-hermes-agent==0.19.0}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--venv-dir PATH] [--python BIN] [--extras LIST] [--run-setup] [--run-start] [--skip-hermes]

Installs teamEvolver from the current repository checkout into a local virtualenv.
Installs Hermes into this project's virtualenv (required for document mining)
unless --skip-hermes is given. A system/global Hermes is never used or changed.

Examples:
  bash scripts/install_teamEvolver.sh
  bash scripts/install_teamEvolver.sh --venv-dir ~/.venvs/teamEvolver --run-setup
  bash scripts/install_teamEvolver.sh --extras all --run-setup --run-start
  bash scripts/install_teamEvolver.sh --skip-hermes   # server won't run mining

Default project install command:
  python -m pip install -e ".[all]"

Hermes provisioning is idempotent and targets only <venv>/bin/hermes. Override
the pinned package spec with TEAMEVOLVER_HERMES_PACKAGE_SPEC when using a mirror
or an internally reviewed build.

After install you can run:
  teamEvolver setup
  teamEvolver start
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv-dir)
      VENV_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --extras)
      INSTALL_EXTRAS="$2"
      shift 2
      ;;
    --run-setup)
      RUN_SETUP=1
      shift
      ;;
    --run-start)
      RUN_START=1
      shift
      ;;
    --skip-hermes)
      SKIP_HERMES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

# Resolve only the Hermes console script owned by this project's virtualenv.
find_hermes_bin() {
  local cand
  for cand in "$VENV_DIR/bin/hermes" "$VENV_DIR/Scripts/hermes.exe"; do
    if [[ -x "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

# Idempotently provision Hermes inside the active project virtualenv. Mining
# shells out to this exact binary; no PATH/system fallback is allowed.
provision_hermes() {
  if [[ "$SKIP_HERMES" -eq 1 ]]; then
    echo "[install_teamEvolver] --skip-hermes set; skipping Hermes CLI provisioning"
    echo "[install_teamEvolver]   (evolve works without it; document mining will be disabled)"
    return 0
  fi

  local existing
  if existing="$(find_hermes_bin)" && "$existing" --version >/dev/null 2>&1; then
    echo "[install_teamEvolver] project Hermes already installed: $existing ($("$existing" --version 2>/dev/null | head -1))"
    return 0
  fi

  echo "[install_teamEvolver] project Hermes not found; installing $HERMES_PACKAGE_SPEC"
  python -m pip install "$HERMES_PACKAGE_SPEC"

  local resolved
  if resolved="$(find_hermes_bin)" && "$resolved" --version >/dev/null 2>&1; then
    echo "[install_teamEvolver] project Hermes installed: $resolved"
  else
    echo "[install_teamEvolver] ERROR: project Hermes is unavailable after installation." >&2
    return 1
  fi
}

echo "[install_teamEvolver] repo root: $ROOT_DIR"
echo "[install_teamEvolver] python: $PYTHON_BIN"
echo "[install_teamEvolver] venv: $VENV_DIR"
echo "[install_teamEvolver] extras: $INSTALL_EXTRAS"

cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
  echo "[install_teamEvolver] pip missing in existing virtualenv; bootstrapping with ensurepip"
  python -m ensurepip --upgrade
fi
python -m pip install -U pip
if [[ "$SKIP_HERMES" -eq 1 && "$INSTALL_EXTRAS" == "all" ]]; then
  # `all` includes the mining/true-replay Hermes dependency. Preserve the
  # documented --skip-hermes behavior for the default invocation.
  INSTALL_EXTRAS="sharing,validation"
  echo "[install_teamEvolver] --skip-hermes: using extras $INSTALL_EXTRAS"
fi
python -m pip install -e ".[${INSTALL_EXTRAS}]"

echo
echo "[install_teamEvolver] provisioning project-isolated Hermes (for document mining)"
provision_hermes

echo
echo "[install_teamEvolver] install complete"
echo "[install_teamEvolver] activate with:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "[install_teamEvolver] next steps:"
echo "  teamEvolver setup"
echo "  teamEvolver start"

if [[ "$RUN_SETUP" -eq 1 ]]; then
  echo
  echo "[install_teamEvolver] running: teamEvolver setup"
  teamEvolver setup
fi

if [[ "$RUN_START" -eq 1 ]]; then
  echo
  echo "[install_teamEvolver] running: teamEvolver start"
  teamEvolver start
fi
