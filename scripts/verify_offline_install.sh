#!/usr/bin/env bash
# Offline-install verification: install app.tar in an empty CPython image
# with networking disabled and no source tree, then check the installed release.
#   scripts/verify_offline_install.sh [app.tar] [linux/arm64|linux/amd64]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARBALL="${1:-$ROOT_DIR/app.tar}"
PLATFORM="${2:-linux/amd64}"
IMAGE="${RUNTIME_IMAGE:-python:3.11-slim-trixie}"
# Docker Desktop shares the user home directory with the VM; /tmp is not shared.
WORK="$(mktemp -d "$HOME/.te-offline.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
tar -xf "$TARBALL" -C "$WORK"

docker run --rm --network none --platform "$PLATFORM" -v "$WORK:/app/deploy:ro" "$IMAGE" sh -ec '
  test "$(ls /app)" = deploy
  python -m venv /opt/te
  PYTHON_BIN=/opt/te/bin/python sh /app/deploy/install.sh
  cd /
  test -z "${PYTHONPATH:-}"
  /opt/te/bin/python - <<PY
import importlib, json, pathlib
import teamEvolver
root = pathlib.Path(teamEvolver.__file__).parent
assert "site-packages" in str(root), root
for name in ("teamEvolver.app", "session_ingestion", "team_skills", "team_replay",
             "team_memory", "team_miner", "asyncpg"):
    importlib.import_module(name)
schema = sorted(p.name for p in (root / "database/schema").glob("*.sql"))
manifest = json.loads(pathlib.Path("/app/deploy/manifest.json").read_text())
assert schema == manifest["schema_versions"], (schema, manifest["schema_versions"])
assert (root / "web/dist/index.html").is_file()
print("installed:", root, "schema:", len(schema), "versions")
PY
  /opt/te/bin/teamEvolver start --help >/dev/null
  echo "[offline] ok"'
