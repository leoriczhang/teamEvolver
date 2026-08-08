#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${SKILLGENE_VENV_DIR:-$ROOT_DIR/.venv}"
INSTALL_EXTRAS="${SKILLGENE_INSTALL_EXTRAS:-all}"
RUN_SETUP=0
RUN_START=0
SKIP_HERMES="${SKILLGENE_SKIP_HERMES:-0}"
# Canonical upstream Hermes installer. Overridable for air-gapped mirrors.
HERMES_INSTALL_URL="${HERMES_INSTALL_URL:-https://hermes-agent.nousresearch.com/install.sh}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--venv-dir PATH] [--python BIN] [--extras LIST] [--run-setup] [--run-start] [--skip-hermes]

Installs SkillGene from the current repository checkout into a local virtualenv.
Also provisions the Hermes CLI (required for the document-mining pipeline) unless
it is already installed or --skip-hermes is given.

Examples:
  bash scripts/install_skillgene.sh
  bash scripts/install_skillgene.sh --venv-dir ~/.venvs/skillgene --run-setup
  bash scripts/install_skillgene.sh --extras all --run-setup --run-start
  bash scripts/install_skillgene.sh --skip-hermes   # server won't run mining

Default install command:
  python -m pip install -e ".[all]"

Hermes provisioning is idempotent: if a working \`hermes\` binary is already on
PATH (or at ~/.local/bin, /opt/homebrew/bin, /usr/local/bin) it is left as-is.
Override the installer source with HERMES_INSTALL_URL for air-gapped mirrors.

After install you can run:
  skillgene setup
  skillgene start
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

# Resolve a working `hermes` binary using the same discovery contract as
# skillgene/skillminer/run_pipeline.py (find_hermes_bin): PATH first, then the
# fixed candidate locations. Prints the resolved path on success.
find_hermes_bin() {
  local cand
  if command -v hermes >/dev/null 2>&1; then
    command -v hermes
    return 0
  fi
  for cand in "$HOME/.local/bin/hermes" "/opt/homebrew/bin/hermes" "/usr/local/bin/hermes"; do
    if [[ -x "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

# Idempotently provision the Hermes CLI. Mining (skillminer) shells out to the
# `hermes` binary, so a server that should run mining needs it. Evolve does NOT
# need it (it calls the LLM HTTP API directly), so --skip-hermes is safe for
# evolve-only deployments.
provision_hermes() {
  if [[ "$SKIP_HERMES" -eq 1 ]]; then
    echo "[install_skillgene] --skip-hermes set; skipping Hermes CLI provisioning"
    echo "[install_skillgene]   (evolve works without it; document mining will be disabled)"
    return 0
  fi

  local existing
  if existing="$(find_hermes_bin)" && "$existing" --version >/dev/null 2>&1; then
    echo "[install_skillgene] Hermes already installed: $existing ($("$existing" --version 2>/dev/null | head -1))"
    return 0
  fi

  echo "[install_skillgene] Hermes CLI not found; installing from $HERMES_INSTALL_URL"
  if ! command -v curl >/dev/null 2>&1; then
    echo "[install_skillgene] WARN: curl not available; cannot auto-install Hermes." >&2
    echo "[install_skillgene]       Install it manually (see https://github.com/NousResearch/hermes-agent)," >&2
    echo "[install_skillgene]       or re-run with --skip-hermes to skip. Mining will be unavailable." >&2
    return 0
  fi

  local installer
  installer="$(mktemp 2>/dev/null || echo "/tmp/hermes-install.$$.sh")"
  if ! curl -fsSL "$HERMES_INSTALL_URL" -o "$installer"; then
    echo "[install_skillgene] WARN: failed to download Hermes installer from $HERMES_INSTALL_URL" >&2
    rm -f "$installer"
    return 0
  fi
  # Non-interactive: no setup wizard, no TTY prompts (server context).
  if bash "$installer" --skip-setup --non-interactive; then
    rm -f "$installer"
  else
    echo "[install_skillgene] WARN: Hermes installer exited non-zero; continuing." >&2
    rm -f "$installer"
  fi

  local resolved
  if resolved="$(find_hermes_bin)" && "$resolved" --version >/dev/null 2>&1; then
    echo "[install_skillgene] Hermes installed: $resolved"
  else
    echo "[install_skillgene] WARN: Hermes still not resolvable after install." >&2
    echo "[install_skillgene]       Ensure ~/.local/bin is on PATH (open a new shell or 'source ~/.bashrc')," >&2
    echo "[install_skillgene]       then verify with 'hermes --version'. Mining needs it; evolve does not." >&2
  fi
}

echo "[install_skillgene] repo root: $ROOT_DIR"
echo "[install_skillgene] python: $PYTHON_BIN"
echo "[install_skillgene] venv: $VENV_DIR"
echo "[install_skillgene] extras: $INSTALL_EXTRAS"

cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install -U pip
python -m pip install -e ".[${INSTALL_EXTRAS}]"

echo
echo "[install_skillgene] provisioning Hermes CLI (for document mining)"
provision_hermes

echo
echo "[install_skillgene] install complete"
echo "[install_skillgene] activate with:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "[install_skillgene] next steps:"
echo "  skillgene setup"
echo "  skillgene start"

if [[ "$RUN_SETUP" -eq 1 ]]; then
  echo
  echo "[install_skillgene] running: skillgene setup"
  skillgene setup
fi

if [[ "$RUN_START" -eq 1 ]]; then
  echo
  echo "[install_skillgene] running: skillgene start"
  skillgene start
fi
