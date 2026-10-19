#!/bin/sh
# Offline install of a TeamEvolver release. Verifies every file against
# manifest.json, installs the hashed dependency lock from the bundled
# wheelhouse, then the application wheel. No index or source tree is used.
set -eu

RELEASE_DIR=${RELEASE_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}
PYTHON_BIN=${PYTHON_BIN:-python}

echo "[install] verifying $RELEASE_DIR/manifest.json"
"$PYTHON_BIN" - "$RELEASE_DIR" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

release = Path(sys.argv[1])
files = json.loads((release / "manifest.json").read_text())["files"]
bad = [name for name, digest in files.items()
       if not (release / name).is_file() or hashlib.sha256((release / name).read_bytes()).hexdigest() != digest]
if bad:
    sys.exit("[install] manifest mismatch: " + ", ".join(bad))
print(f"[install] {len(files)} files verified")
PY

# Build tools first: locked sdists in the wheelhouse are built offline with them.
"$PYTHON_BIN" -m pip install --no-index --find-links="$RELEASE_DIR/wheels" \
  --require-hashes -r "$RELEASE_DIR/build-requirements.lock"
"$PYTHON_BIN" -m pip install --no-index --no-build-isolation --find-links="$RELEASE_DIR/wheels" \
  --require-hashes -r "$RELEASE_DIR/requirements.lock"
"$PYTHON_BIN" -m pip install --no-index --no-deps "$RELEASE_DIR"/teamevolver-*.whl
"$PYTHON_BIN" -m pip check
echo "[install] finished"
