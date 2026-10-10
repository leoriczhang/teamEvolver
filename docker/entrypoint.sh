#!/bin/sh
set -eu

port="${TEAMEVOLVER_PORT:-52010}"

# The CLI deliberately requires an explicit config file. Initialise defaults
# on first start, then keep the bind-mounted configuration across upgrades.
if [ ! -f "$HOME/.teamEvolver/config.yaml" ]; then
  teamEvolver config service.host 0.0.0.0
  teamEvolver config service.port "$port"
fi

exec teamEvolver start --port "$port"
