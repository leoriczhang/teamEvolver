#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
LIFT_TARGET="${SKILLGENE_LIFT_ROOT:-$REPO_ROOT/external/LIFT}"
LIFT_SOURCE_URL="${LIFT_GIT_URL:-https://github.com/FeiZhuNiU-INFJA/LIFT.git}"
LIFT_SOURCE_REF="${LIFT_GIT_REF:-ed8c9d750d729e4c5b1bbf237dd8483d9d142689}"
INSTALL_DEPS=0
LIFT_PYTHON_BIN="${SKILLGENE_LIFT_BOOTSTRAP_PYTHON:-python3.12}"

if [[ "${1:-}" == "--install-deps" ]]; then
  INSTALL_DEPS=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $(basename "$0") [--install-deps]" >&2
  exit 2
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to set up LIFT" >&2
  exit 1
fi

mkdir -p "$(dirname "$LIFT_TARGET")"
if [[ -d "$LIFT_TARGET/.git" ]]; then
  if [[ -n "$(git -C "$LIFT_TARGET" status --porcelain)" ]]; then
    echo "Refusing to update a dirty LIFT checkout: $LIFT_TARGET" >&2
    exit 1
  fi
  git -C "$LIFT_TARGET" fetch --depth 1 origin "$LIFT_SOURCE_REF"
  git -C "$LIFT_TARGET" checkout --detach FETCH_HEAD
else
  if [[ -e "$LIFT_TARGET" ]]; then
    echo "Target exists but is not a git checkout: $LIFT_TARGET" >&2
    exit 1
  fi
  git clone --no-checkout "$LIFT_SOURCE_URL" "$LIFT_TARGET"
  git -C "$LIFT_TARGET" fetch --depth 1 origin "$LIFT_SOURCE_REF"
  git -C "$LIFT_TARGET" checkout --detach FETCH_HEAD
fi

echo "LIFT checkout ready: $LIFT_TARGET"
echo "Revision: $(git -C "$LIFT_TARGET" rev-parse HEAD)"
echo "Set SKILLGENE_LIFT_ROOT=$LIFT_TARGET when SkillGene runs outside this source checkout."

if [[ "$INSTALL_DEPS" -eq 1 ]]; then
  if ! command -v "$LIFT_PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3.12 executable not found: $LIFT_PYTHON_BIN" >&2
    exit 1
  fi
  "$LIFT_PYTHON_BIN" -m venv "$LIFT_TARGET/.venv-skillgene"
  "$LIFT_TARGET/.venv-skillgene/bin/python" -m pip install -U pip
  "$LIFT_TARGET/.venv-skillgene/bin/python" -m pip install -r "$LIFT_TARGET/requirements.txt"
  echo "Set SKILLGENE_LIFT_PYTHON=$LIFT_TARGET/.venv-skillgene/bin/python"
fi

cat <<'EOF'

LIFT full runs also require Docker, Langfuse, provider credentials and a built
agent runtime image. For Hermes, follow agent-runtimes/hermes/README.md and use
the serial_single warmup policy (SkillGene adds that flag automatically).

The upstream checkout is kept external because the referenced repository does
not currently publish a license file. Review its terms before redistribution.
EOF
