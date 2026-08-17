#!/bin/sh
set -eu

port="${TEAMEVOLVER_PORT:-52010}"
skillminer_root="/app/teamEvolver/skillminer"

# Bind mounts start empty on a fresh host. Create the layout expected by the
# embedded SkillMiner service before teamEvolver starts.
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

# The CLI deliberately requires an explicit config file. Initialise defaults
# on first start, then keep the bind-mounted configuration across upgrades.
if [ ! -f "$HOME/.teamEvolver/config.yaml" ]; then
  teamEvolver config service.host 0.0.0.0
  teamEvolver config service.port "$port"
fi

exec teamEvolver start --port "$port"
