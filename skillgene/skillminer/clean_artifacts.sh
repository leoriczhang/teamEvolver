#!/usr/bin/env bash
set -euo pipefail

# 删除流水线与评测运行产物，始终保留 data/input/ 中的原始文档。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]]; then
  DRY_RUN=1
fi

ARTIFACTS=(
  "sample_packages"
  "semantic_reports"
  "compiled_skill"
  "reflection_rounds"
  "run_history"
  "benchmark_results"
  "benchmark_sessions"
  "coverage_reports"
  "skill_test_results"
  "lift_datasets"
  "__pycache__"
)

remove_path() {
  local path="$1"
  if [[ "$path" == "data" || "$path" == data/* ]]; then
    echo "拒绝删除输入数据路径：$path" >&2
    exit 1
  fi
  if [[ ! -e "$path" ]]; then
    return
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $path"
  else
    rm -rf "$path"
    echo "[removed] $path"
  fi
}

for path in "${ARTIFACTS[@]}"; do
  remove_path "$path"
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  find . -type d -name "__pycache__" -not -path "./data/*" -prune -exec rm -rf {} +
fi
