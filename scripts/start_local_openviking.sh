#!/usr/bin/env bash
set -euo pipefail

OPENVIKING_REPO="${OPENVIKING_REPO:-$HOME/OpenViking}"
OPENVIKING_CONFIG="${OPENVIKING_CONFIG:-$HOME/.openviking/ov.conf}"
OPENVIKING_PYTHON="${OPENVIKING_PYTHON:-}"
BUILD_STUDIO="${OPENVIKING_BUILD_STUDIO:-1}"

python_ready() {
  local candidate="$1"
  [[ -x "$candidate" ]] && \
    PYTHONPATH="$OPENVIKING_REPO${PYTHONPATH:+:$PYTHONPATH}" \
      "$candidate" -c "import tree_sitter_language_pack" >/dev/null 2>&1
}

if [[ ! -d "$OPENVIKING_REPO/.git" ]]; then
  echo "OpenViking checkout not found: $OPENVIKING_REPO" >&2
  exit 1
fi
if [[ ! -f "$OPENVIKING_CONFIG" ]]; then
  echo "OpenViking config not found: $OPENVIKING_CONFIG" >&2
  echo "Run: openviking-server init" >&2
  exit 1
fi

if [[ -z "$OPENVIKING_PYTHON" ]]; then
  if python_ready "$OPENVIKING_REPO/.venv/bin/python"; then
    OPENVIKING_PYTHON="$OPENVIKING_REPO/.venv/bin/python"
  elif python_ready "$HOME/miniconda3/envs/openviking/bin/python"; then
    OPENVIKING_PYTHON="$HOME/miniconda3/envs/openviking/bin/python"
  else
    OPENVIKING_PYTHON="$(command -v python3 || command -v python)"
  fi
fi

if ! python_ready "$OPENVIKING_PYTHON"; then
  echo "The selected Python environment is not synchronized with the latest OpenViking source." >&2
  echo "Install it with:" >&2
  echo "  $OPENVIKING_PYTHON -m pip install --index-url https://mirrors.aliyun.com/pypi/simple cmake 'tree-sitter-language-pack>=1.12,<2'" >&2
  echo "  $OPENVIKING_PYTHON -m pip install --no-build-isolation -e $OPENVIKING_REPO --extra-index-url https://mirrors.aliyun.com/pypi/simple" >&2
  exit 1
fi

STUDIO_DIR="$OPENVIKING_REPO/web-studio/dist"
if [[ "$BUILD_STUDIO" == "1" ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to build OpenViking Web Studio" >&2
    exit 1
  fi
  echo "[OpenViking] building Web Studio..."
  npm --prefix "$OPENVIKING_REPO/web-studio" ci
  # The bundle is served under /studio, so it must be built with that base or
  # index.html will reference /assets/* (404) and the SPA never boots. Always
  # rebuild so studio source changes take effect on restart.
  npm --prefix "$OPENVIKING_REPO/web-studio" run build -- --base=/studio/
fi

export OPENVIKING_WEB_STUDIO_DIR="$STUDIO_DIR"
export PYTHONPATH="$OPENVIKING_REPO${PYTHONPATH:+:$PYTHONPATH}"

echo "[OpenViking] repo:   $OPENVIKING_REPO"
echo "[OpenViking] config: $OPENVIKING_CONFIG"
echo "[OpenViking] python: $OPENVIKING_PYTHON"
echo "[OpenViking] studio: http://127.0.0.1:1933/studio/"

cd "$OPENVIKING_REPO"
exec "$OPENVIKING_PYTHON" -m openviking_cli.server_bootstrap \
  --config "$OPENVIKING_CONFIG" "$@"
