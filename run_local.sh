#!/bin/bash
# 本地启动 teamEvolver（前台运行，Ctrl-C 退出）
# 配置：首次启动用 config/config.yaml 播种到 runtime/config/，之后以 runtime/config 为准
#       （与生产容器 container_start.sh 的目录结构同构）
# 端口：默认 52010，可用 ./run_local.sh 52013 或 PORT=52013 覆盖
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# 本地 .env（可选，模板见 .env.example）：TEAMEVOLVER_PG_* 等
# 环境变量，与部署侧挂载 /app/deploy/.env 的加载方式一致。
if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    . "$ROOT_DIR/.env"
    set +a
fi

PORT="${1:-${PORT:-52010}}"
RUNTIME_DIR="$ROOT_DIR/runtime"
CONFIG_DIR="$RUNTIME_DIR/config"

if [ ! -x ".venv/bin/teamEvolver" ]; then
    echo "未找到 .venv，请先执行:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[all]'"
    exit 1
fi

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ] && [ -f "$ROOT_DIR/config/config.yaml" ]; then
    cp "$ROOT_DIR/config/config.yaml" "$CONFIG_DIR/config.yaml"
fi
# 复制 .env 文件（密钥）到运行配置目录
if [ ! -f "$CONFIG_DIR/.env" ] && [ -f "$ROOT_DIR/config/.env" ]; then
    cp "$ROOT_DIR/config/.env" "$CONFIG_DIR/.env"
    chmod 600 "$CONFIG_DIR/.env"
fi
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

echo "Starting teamEvolver on port $PORT..."
.venv/bin/teamEvolver start --port "$PORT" &
SERVER_PID=$!

echo ""
echo "Console:  http://127.0.0.1:$PORT/"
echo "Config:   $CONFIG_DIR/config.yaml"
echo ""

wait "$SERVER_PID"
