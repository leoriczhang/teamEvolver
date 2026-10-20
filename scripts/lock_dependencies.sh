#!/usr/bin/env bash
# Regenerate the hashed dependency locks from pyproject.toml (the only source of
# direct dependencies). The runtime target is CPython 3.11 on Linux amd64/arm64.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
command -v uv >/dev/null 2>&1 || { echo "[lock] uv is required" >&2; exit 1; }

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

compile() {
  uv pip compile "$1" --universal --python-version 3.11 --generate-hashes \
    --custom-compile-command "scripts/lock_dependencies.sh" -o "$2"
}

compile pyproject.toml requirements.lock
# uv records the input path; generate the build-system input under a stable name.
python3 - "$TMP_DIR/build-system.in" <<'PY'
import sys
import tomllib

with open("pyproject.toml", "rb") as handle:
    requires = tomllib.load(handle)["build-system"]["requires"]
open(sys.argv[1], "w").write("\n".join(requires) + "\n")
PY
(cd "$TMP_DIR" && compile build-system.in "$ROOT_DIR/build-requirements.lock")
