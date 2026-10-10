#!/bin/sh

set -eu

echo '************************ Package Start ***********************************'

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RELEASE_DIR="$ROOT_DIR/release"
WHEELHOUSE_DIR="$ROOT_DIR/site-packages"
APP_TAR="$ROOT_DIR/app.tar"
PYTHON_BIN=${PYTHON_BIN:-}

# 注意：构建机 Python 大版本必须与容器运行时一致，否则带 C 扩展的依赖
# （pydantic-core、uvloop 等）的 wheel ABI 不匹配，离线安装会失败。
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.13 >/dev/null 2>&1; then
    PYTHON_BIN=python3.13
  else
    PYTHON_BIN=python3
  fi
fi

echo "Using Python: $($PYTHON_BIN --version)"

rm -rf "$RELEASE_DIR" "$WHEELHOUSE_DIR" "$APP_TAR" "$ROOT_DIR/build"
mkdir -p "$RELEASE_DIR" "$WHEELHOUSE_DIR"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
copy_dir() {
  src="$1"
  dest="$2"
  if [ -d "$ROOT_DIR/$src" ]; then
    mkdir -p "$(dirname "$RELEASE_DIR/$dest")"
    cp -R "$ROOT_DIR/$src" "$RELEASE_DIR/$dest"
  fi
}

copy_file() {
  src="$1"
  dest="$2"
  if [ -f "$ROOT_DIR/$src" ]; then
    mkdir -p "$(dirname "$RELEASE_DIR/$dest")"
    cp "$ROOT_DIR/$src" "$RELEASE_DIR/$dest"
  fi
}

# ---------------------------------------------------------------------------
# 1. Copy application source (includes pre-built web/dist console assets)
# ---------------------------------------------------------------------------
test -s "$ROOT_DIR/teamEvolver/web/dist/index.html"
copy_dir  "teamEvolver"      "teamEvolver"
copy_dir  "team_skills"      "team_skills"
copy_dir  "team_memory"      "team_memory"
copy_dir  "team_miner"       "team_miner"
copy_dir  "team_replay"      "team_replay"
copy_dir  "team_ontology"    "team_ontology"
copy_dir  "session_ingestion" "session_ingestion"

# ---------------------------------------------------------------------------
# 2. Copy packaging metadata (referenced by pyproject.toml wheel build)
# ---------------------------------------------------------------------------
copy_file "pyproject.toml"   "pyproject.toml"
copy_file "README.md"        "README.md"
copy_file "README.en.md"     "README.en.md"
copy_file "LICENSE"          "LICENSE"

# ---------------------------------------------------------------------------
# 3. Copy deployment scripts & requirements
# ---------------------------------------------------------------------------
copy_file "cicd_scripts/install.sh"         "install.sh"
copy_file "container_start.sh"                "container_start.sh"
copy_file "requirements.txt"                "requirements.txt"

# ---------------------------------------------------------------------------
# 4. Clean up bytecode / OS artifacts / local runtime state
# ---------------------------------------------------------------------------
find "$RELEASE_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$RELEASE_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
find "$RELEASE_DIR" -type f -name ".DS_Store" -delete

# ---------------------------------------------------------------------------
# 5. Build teamEvolver wheel & download wheels for offline install
# ---------------------------------------------------------------------------
"$PYTHON_BIN" -m pip wheel --no-deps -w "$WHEELHOUSE_DIR" "$ROOT_DIR"
"$PYTHON_BIN" -m pip wheel -r "$ROOT_DIR/requirements.txt" -w "$WHEELHOUSE_DIR"

cp -R "$WHEELHOUSE_DIR" "$RELEASE_DIR/site-packages"

# ---------------------------------------------------------------------------
# 6. Make scripts executable & create tarball
# ---------------------------------------------------------------------------
chmod +x "$RELEASE_DIR/install.sh" "$RELEASE_DIR/container_start.sh"

cd "$RELEASE_DIR"
tar -cvf "$APP_TAR" ./*

echo ""
echo "Release package created: $APP_TAR"
