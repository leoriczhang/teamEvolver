#!/bin/sh
# Container entrypoint: run the installed release as one role.
#   TEAMEVOLVER_ROLE=api     HTTP API + console (default port 52010)
#   TEAMEVOLVER_ROLE=worker  task Worker + health endpoint (default port 52011)
# Optional /app/deploy/.env and /app/deploy/config/config.yaml are read-only
# bootstrap inputs; mutable state lives in PostgreSQL.
set -eu

if [ -f /app/deploy/.env ]; then
    set -a
    . /app/deploy/.env
    set +a
fi

config_dir="${TEAMEVOLVER_CONFIG_DIR:-/app/deploy/config}"
if [ -z "${TEAMEVOLVER_CONFIG_FILE:-}" ] && [ -f "$config_dir/config.yaml" ]; then
    export TEAMEVOLVER_CONFIG_FILE="$config_dir/config.yaml"
fi
export TEAMEVOLVER_API_HOST="${TEAMEVOLVER_API_HOST:-0.0.0.0}"

role="${TEAMEVOLVER_ROLE:-}"
case "$role" in
    api|worker) ;;
    *) echo "TEAMEVOLVER_ROLE must be api or worker (got '${role}')" >&2; exit 64 ;;
esac

port="${APP_PORT:-${TEAMEVOLVER_PORT:-}}"
if [ -n "$port" ]; then
    exec teamEvolver start --role "$role" --port "$port"
fi
exec teamEvolver start --role "$role"
