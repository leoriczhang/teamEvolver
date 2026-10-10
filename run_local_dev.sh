#!/bin/bash
# 带环境选择的本地启动脚本。
#
# 用法:
#   ./run_local_dev.sh                      # 默认 env=local (读 config/config_local.yaml + config/.env_local)
#   ./run_local_dev.sh local                # 等价
#   ./run_local_dev.sh sit 52013            # env=sit, port=52013
#   ENV=local PORT=52010 ./run_local_dev.sh # 环境变量方式
#
# 对比 run_local.sh：原脚本固定播种 config/config.yaml → runtime/config/config.yaml；
# 本脚本按 ENV 名读 config/config_${ENV}.yaml（不存在则回退到 config.yaml），
# 并把对应的 config/.env_${ENV} 作为运行时 .env 使用。
# 不改动 config/config.yaml、config/.env 等既有产物。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

ENV_NAME="${1:-${ENV:-local}}"
PORT="${2:-${PORT:-52010}}"

SRC_CONFIG="$ROOT_DIR/config/config_${ENV_NAME}.yaml"
SRC_ENV="$ROOT_DIR/config/.env_${ENV_NAME}"

if [ ! -f "$SRC_CONFIG" ]; then
    echo "未找到 $SRC_CONFIG，可用环境：" >&2
    ls "$ROOT_DIR/config" | sed -n 's/^config_\(.*\)\.yaml$/  - \1/p' >&2
    exit 1
fi
if [ ! -f "$SRC_ENV" ]; then
    echo "未找到 $SRC_ENV" >&2
    exit 1
fi

# 加载 env 到当前 shell（供 teamEvolver CLI / 子进程使用），与 run_local.sh 一致
set -a
# shellcheck disable=SC1090
. "$SRC_ENV"
set +a

if [ ! -x ".venv/bin/teamEvolver" ]; then
    echo "未找到 .venv，请先执行:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[all]'" >&2
    exit 1
fi

# 每个环境独立 runtime 目录，互不污染
RUNTIME_DIR="$ROOT_DIR/runtime_${ENV_NAME}"
CONFIG_DIR="$RUNTIME_DIR/config"
mkdir -p "$CONFIG_DIR"

# 播种配置：每次启动都覆盖，确保 config_${ENV}.yaml 变更会被带入
cp "$SRC_CONFIG" "$CONFIG_DIR/config.yaml"
cp "$SRC_ENV" "$CONFIG_DIR/.env"
chmod 600 "$CONFIG_DIR/.env"

# ConfigStore 优先读 $HOME/.teamEvolver/config.yaml，通过 HOME 重定向到 runtime 目录
if [ ! -e "$RUNTIME_DIR/.teamEvolver" ]; then
    ln -s "$CONFIG_DIR" "$RUNTIME_DIR/.teamEvolver"
fi

export HOME="$RUNTIME_DIR"
.venv/bin/teamEvolver config service.port "$PORT"
.venv/bin/teamEvolver config evolve.server_url "http://127.0.0.1:$PORT"

cleanup() {
    echo "Shutting down..."
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

echo "Starting teamEvolver (env=$ENV_NAME) on port $PORT..."
.venv/bin/teamEvolver start --port "$PORT" &
SERVER_PID=$!

echo ""
echo "Env:      $ENV_NAME"
echo "Console:  http://127.0.0.1:$PORT/"
echo "Config:   $CONFIG_DIR/config.yaml"
echo "Runtime:  $RUNTIME_DIR"
echo ""

wait "$SERVER_PID"
