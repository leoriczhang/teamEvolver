#!/bin/sh

set -eu

cd /app/deploy

# Optional env file: if the platform mounts one at /app/deploy/.env instead of
# (or in addition to) injecting env vars, load it before validation (same
# mechanism as the OpenViking container_start.sh). Sourcing semantics keep
# full-line and trailing # comments inert, so leftover "KEY=  # TODO" lines
# stay empty and fail the check below loudly.
if [ -f /app/deploy/.env ]; then
  set -a
  . /app/deploy/.env
  set +a
fi

# 端口：平台显式注入 APP_PORT / TEAMEVOLVER_PORT 时覆盖配置；
# 否则以配置文件中的 service.port 为准（如挂载的 SIT 配置已写好 1080）。
port="${APP_PORT:-${TEAMEVOLVER_PORT:-}}"

# 服务主进程 HOME（.hermes/skills 等运行目录）。
export HOME="${TEAMEVOLVER_HOME:-/app/deploy/runtime}"
mkdir -p "$HOME"

# 配置目录：平台把环境配置挂载到这里。
# 实际部署（SIT/PRD）平台按 config.yaml 与 .env 挂载，不带环境后缀，
# 且为只读单文件挂载（所在目录可写，挂载的文件本身只读）。
# 可写挂载时整个配置目录即唯一真源——config.yaml（服务配置）、
# users.json（控制台账号）、会话与备份都落在卷上，控制台修改直接写回。
# 只读挂载时挂载内容仅作种子，服务与 CLI 的读写都切到 $HOME/.teamEvolver
# 运行副本（需持久化 TEAMEVOLVER_HOME 才能保留控制台修改）。
CONFIG_DIR="${TEAMEVOLVER_CONFIG_DIR:-/app/deploy/config}"
MOUNTED_CONFIG_DIR="$CONFIG_DIR"
mkdir -p "$CONFIG_DIR"

# 确定服务实际读取的配置文件名：
# - TEAMEVOLVER_ENV=sit/prd 且 config_{env}.yaml 确实存在 → 用后缀名
# - 其余情况（含平台按 config.yaml / .env 无后缀挂载）→ config.yaml
# 后缀文件不存在时回落 config.yaml，与服务的 ConfigStore 解析优先级一致。
CONFIG_BASENAME="config.yaml"
if [ -n "${TEAMEVOLVER_ENV:-}" ] && [ -f "$MOUNTED_CONFIG_DIR/config_${TEAMEVOLVER_ENV}.yaml" ]; then
  CONFIG_BASENAME="config_${TEAMEVOLVER_ENV}.yaml"
fi

if [ -f "$MOUNTED_CONFIG_DIR/$CONFIG_BASENAME" ] && touch "$MOUNTED_CONFIG_DIR/$CONFIG_BASENAME" 2>/dev/null; then
  # 可写挂载：配置目录作为服务的配置与状态目录。
  [ -e "$HOME/.teamEvolver" ] || ln -s "$CONFIG_DIR" "$HOME/.teamEvolver"
else
  # 只读挂载（或未挂载）：config.yaml 是只读挂载点，rename 与原地写都会
  # EBUSY/EROFS，服务与 CLI 的所有写路径必须切到可写的运行目录副本。
  mkdir -p "$HOME/.teamEvolver"
  if [ ! -f "$HOME/.teamEvolver/config.yaml" ] && [ -f "$MOUNTED_CONFIG_DIR/$CONFIG_BASENAME" ]; then
    # 仅首次种子：后续重启保留控制台在副本上的修改。
    cp "$MOUNTED_CONFIG_DIR/$CONFIG_BASENAME" "$HOME/.teamEvolver/config.yaml"
    chmod 600 "$HOME/.teamEvolver/config.yaml"
  fi
  # .env（密钥）每次启动从挂载刷新，避免平台轮换后副本过期。
  if [ -n "${TEAMEVOLVER_ENV:-}" ] && [ -f "$MOUNTED_CONFIG_DIR/.env_${TEAMEVOLVER_ENV}" ]; then
    cp "$MOUNTED_CONFIG_DIR/.env_${TEAMEVOLVER_ENV}" "$HOME/.teamEvolver/.env_${TEAMEVOLVER_ENV}"
    chmod 600 "$HOME/.teamEvolver/.env_${TEAMEVOLVER_ENV}"
  elif [ -f "$MOUNTED_CONFIG_DIR/.env" ]; then
    cp "$MOUNTED_CONFIG_DIR/.env" "$HOME/.teamEvolver/.env"
    chmod 600 "$HOME/.teamEvolver/.env"
  fi
  CONFIG_DIR="$HOME/.teamEvolver"
fi
export TEAMEVOLVER_CONFIG_DIR="$CONFIG_DIR"

# 优先加载 /app/deploy 下的源码而非 pip 安装的 wheel 副本：
# 使挂载的 SkillMiner 数据目录生效，并保证主服务与内嵌子进程版本一致。
export PYTHONPATH="/app/deploy${PYTHONPATH:+:$PYTHONPATH}"

# OpenViking 服务通过控制台配置远程 endpoint 访问，无本地依赖。
# 如需控制台内嵌的 OpenViking CLI 面板，可挂载 ov 二进制并通过
# OPENVIKING_CLI_BIN 指向它。

# SkillMiner 的 find_hermes_bin 只认 TEAMEVOLVER_HERMES_BIN，不回退 PATH。
if [ -z "${TEAMEVOLVER_HERMES_BIN:-}" ] && command -v hermes >/dev/null 2>&1; then
  TEAMEVOLVER_HERMES_BIN="$(command -v hermes)"
  export TEAMEVOLVER_HERMES_BIN
fi

# Fail fast when the mounted config enables storage_pg but no PG credentials
# are reachable: an unset var would otherwise surface much later as a pool
# bootstrap crash with a confusing connection error (mirrors the OpenViking
# required-vars check). The DSN may come from storage_pg.dsn in the config or
# from a complete TEAMEVOLVER_PG_* env group (see .env_sit/.env_prd).
if [ -f "$CONFIG_DIR/$CONFIG_BASENAME" ]; then
  python3 - "$CONFIG_DIR/$CONFIG_BASENAME" <<'PY' || exit 1
import os
import sys

import yaml

with open(sys.argv[1], encoding="utf-8-sig") as handle:
    data = yaml.safe_load(handle) or {}
pg = data.get("storage_pg") or {}
if not pg.get("enabled") or str(pg.get("dsn") or "").strip():
    sys.exit(0)

missing = [
    f"TEAMEVOLVER_PG_{field}"
    for field in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD")
    if not str(os.environ.get(f"TEAMEVOLVER_PG_{field}") or "").strip()
]
if missing:
    print(
        "ERROR: storage_pg.enabled is set but the TEAMEVOLVER_PG_* env group "
        f"is incomplete (missing: {', '.join(missing)}). Paste the PG section "
        "from .env_sit/.env_prd into the deployment env config or mount it at "
        "/app/deploy/.env.",
        file=sys.stderr,
    )
    sys.exit(1)
PY
fi

# Bind mounts start empty on a fresh host. Create the layout expected by the
# embedded SkillMiner service before teamEvolver starts.
skillminer_root="/app/deploy/team_miner"
for relative_path in \
  "data/input" \
  ".knowledge_originals" \
  "mining_jobs" \
  "sample_packages" \
  "semantic_reports" \
  "compiled_skill" \
  "benchmark_results" \
  "reflection_rounds" \
  "run_history" \
  "lift_datasets"; do
  mkdir -p "${skillminer_root}/${relative_path}"
done

# CLI 需要显式配置文件存在；种子阶段未生成时兜底创建。
# 服务在容器内必须监听 0.0.0.0。
# 守卫检查 CLI 实际解析的文件（env 模式下后缀文件缺失时回落 config.yaml），
# 避免把兜底端口 52010 覆写进已存在的挂载/副本配置。
if [ ! -f "$CONFIG_DIR/$CONFIG_BASENAME" ] && [ ! -f "$HOME/.teamEvolver/config.yaml" ]; then
  teamEvolver config service.port 52010
fi
teamEvolver config service.host 0.0.0.0

# Foreground start: logs go to stdout for container log collection.
# 注入端口时同步改写配置并显式指定；未注入时直接用配置文件的端口。
if [ -n "$port" ]; then
  teamEvolver config service.port "$port"
  teamEvolver config evolve.server_url "http://127.0.0.1:$port"
  exec teamEvolver start --port "$port"
fi
exec teamEvolver start
