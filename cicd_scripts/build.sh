#!/bin/sh
# Build the immutable offline release:
#
#   release/
#     teamEvolver-<version>-py3-none-any.whl   application (Python packages,
#                                               schema SQL, console assets)
#     wheels/                                   hashed dependency wheelhouse;
#                                               source-only dependencies keep
#                                               their locked sdist
#     requirements.lock, build-requirements.lock
#     install.sh, container_start.sh
#     manifest.json                             versions + SHA-256 of every file
#
# The wheelhouse targets the runtime image (CPython 3.11, manylinux glibc);
# TARGET_ARCH selects x86_64 (default) or aarch64. Every artifact matches a
# hash in a lock file. Source-only dependencies are built at install time from
# their locked sdist with the locked build tools, still without network; the
# application wheel is built here inside the runtime image without network.
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RELEASE_DIR="$ROOT_DIR/release"
STAGE_DIR="$ROOT_DIR/.release-stage"
APP_TAR="$ROOT_DIR/app.tar"
TARGET_ARCH=${TARGET_ARCH:-x86_64}
RUNTIME_IMAGE=${RUNTIME_IMAGE:-python:3.11-slim-trixie}
PYTHON_BIN=${PYTHON_BIN:-python3}

case "$TARGET_ARCH" in
  x86_64) docker_platform=linux/amd64 ;;
  aarch64) docker_platform=linux/arm64 ;;
  *) echo "unsupported TARGET_ARCH: $TARGET_ARCH" >&2; exit 1 ;;
esac

echo "[build] frontend: clean install, test, production build"
if [ "${SKIP_FRONTEND:-0}" = 1 ]; then
  echo "[build] SKIP_FRONTEND=1: reusing teamEvolver/web/dist"
else
  rm -rf "$ROOT_DIR/web-ui/node_modules"
  npm --prefix "$ROOT_DIR/web-ui" ci
  npm --prefix "$ROOT_DIR/web-ui" test
  npm --prefix "$ROOT_DIR/web-ui" run build
fi
test -s "$ROOT_DIR/teamEvolver/web/dist/index.html"

rm -rf "$RELEASE_DIR" "$STAGE_DIR" "$APP_TAR"
mkdir -p "$RELEASE_DIR/wheels" "$STAGE_DIR/src"

# Lock markers compare the full interpreter version; resolve against the image.
runtime_python=$(docker run --rm --platform "$docker_platform" "$RUNTIME_IMAGE" \
  python -c 'import platform; print(platform.python_version())')

echo "[build] hashed wheelhouse for CPython $runtime_python manylinux $TARGET_ARCH"
"$PYTHON_BIN" - "$PYTHON_BIN" "$TARGET_ARCH" "$runtime_python" "$RELEASE_DIR/wheels" \
  "$ROOT_DIR/requirements.lock" "$ROOT_DIR/build-requirements.lock" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

from pip._vendor.packaging.markers import Marker

python, arch, version, wheels, *locks = sys.argv[1:]
binary = ["--only-binary=:all:", "--python-version", version, "--implementation", "cp",
          "--abi", "cp311", "--abi", "abi3", "--abi", "none"]
for tag in ("manylinux_2_28", "manylinux_2_17", "manylinux2014"):
    binary += ["--platform", f"{tag}_{arch}"]
# pip evaluates lock markers against the build host; select for the image instead.
target = {"python_version": version.rsplit(".", 1)[0], "python_full_version": version,
          "sys_platform": "linux", "platform_system": "Linux", "platform_machine": arch,
          "platform_python_implementation": "CPython", "implementation_name": "cpython",
          "os_name": "posix"}


def blocks(path):
    for block in re.split(r"\n(?=[A-Za-z])", Path(path).read_text()):
        head, _, rest = block.partition("\n")
        if not re.match(r"[A-Za-z0-9_.\-]+==", head):
            continue
        requirement, _, marker = head.rstrip(" \\").partition(";")
        if marker.strip() and not Marker(marker.strip()).evaluate(target):
            continue
        pinned = requirement.strip()
        yield pinned.lower(), (pinned + " \\\n" + rest).rstrip()


def download(entries, extra, dest):
    lock = Path(dest) / ".lock.txt"
    lock.write_text("\n".join(entries.values()) + "\n")
    result = subprocess.run([python, "-m", "pip", "download", "--quiet", "--no-deps", "--require-hashes",
                             "-d", dest, *extra, "-r", str(lock)], capture_output=True, text=True)
    lock.unlink()
    return result


entries = {name: block for path in locks for name, block in blocks(path)}
source_only = {}
while True:
    result = download(entries, binary, wheels)
    if result.returncode == 0:
        break
    missing = re.search(r"No matching distribution found for ([A-Za-z0-9_.\-]+==[^\s;]+)", result.stderr)
    if not missing:
        sys.exit(result.stderr)
    name = missing.group(1).lower()
    source_only[name] = entries.pop(name)
if source_only:
    result = download(source_only, ["--no-binary=:all:"], wheels)
    if result.returncode:
        sys.exit(result.stderr)
print("[build] source-only dependencies:", sorted(source_only) or "none")
PY

echo "[build] application source snapshot"
for path in pyproject.toml README.md LICENSE teamEvolver team_skills team_memory team_miner \
  team_replay session_ingestion; do
  cp -R "$ROOT_DIR/$path" "$STAGE_DIR/src/$path"
done
find "$STAGE_DIR/src" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$STAGE_DIR/src" -type f \( -name "*.pyc" -o -name ".DS_Store" \) -delete
cp "$ROOT_DIR/build-requirements.lock" "$STAGE_DIR/"

echo "[build] offline application wheel build in $RUNTIME_IMAGE ($docker_platform)"
docker run --rm --platform "$docker_platform" --network none \
  -v "$STAGE_DIR:/stage" -v "$RELEASE_DIR:/release" "$RUNTIME_IMAGE" sh -ec '
    python -m pip install --quiet --no-index --find-links=/release/wheels \
      --require-hashes -r /stage/build-requirements.lock
    python -m pip wheel --quiet --no-index --no-deps --no-build-isolation -w /release /stage/src'
rm -rf "$STAGE_DIR"

cp "$ROOT_DIR/requirements.lock" "$ROOT_DIR/build-requirements.lock" "$RELEASE_DIR/"
cp "$ROOT_DIR/cicd_scripts/install.sh" "$ROOT_DIR/container_start.sh" "$RELEASE_DIR/"
chmod +x "$RELEASE_DIR/install.sh" "$RELEASE_DIR/container_start.sh"

echo "[build] manifest"
"$PYTHON_BIN" - "$ROOT_DIR" "$RELEASE_DIR" "$TARGET_ARCH" "$RUNTIME_IMAGE" <<'PY'
import hashlib
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

root, release, arch, image = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
project = tomllib.loads((root / "pyproject.toml").read_text())["project"]


def tool(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


manifest = {
    "name": project["name"],
    "version": project["version"],
    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "target": {"python": "cp311", "platform": f"manylinux_{arch}", "runtime_image": image,
               "runtime_image_digest": tool("docker", "image", "inspect", "--format",
                                            "{{index .RepoDigests 0}}", image)},
    "schema_versions": sorted(p.name for p in (root / "teamEvolver/database/schema").glob("*.sql")),
    "toolchain": {"node": tool("node", "--version"), "npm": tool("npm", "--version")},
    "files": {str(p.relative_to(release)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(release.rglob("*")) if p.is_file() and p.name != "manifest.json"},
}
(release / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
print(f"[build] {len(manifest['files'])} files recorded")
PY

tar -C "$RELEASE_DIR" -cf "$APP_TAR" .
echo "[build] release package: $APP_TAR"
