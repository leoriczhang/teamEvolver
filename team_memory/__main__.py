"""Local CLI for the unified Team Memory workflow."""

import argparse
import json

from teamEvolver.config_store import ConfigStore
from team_memory.service import MemoryAggregationService


def main():
    parser = argparse.ArgumentParser(description="Team Memory compile workflow")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--pipeline", choices=["both", "aggregate", "maintain"], default="maintain")
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--last-compile-time",
        default="",
        help="Only recompile sources modified at/after this ISO time or Unix timestamp",
    )
    args = parser.parse_args()
    config = ConfigStore().to_config()
    service = MemoryAggregationService(config)
    if args.status:
        print(json.dumps(service.list_runs(), ensure_ascii=False, indent=2))
        return
    key = config.sharing_viking_team_api_key or config.sharing_viking_api_key
    if not key:
        parser.error("Configure the OpenViking service credential first")
    try:
        run = service.new_run(
            config.sharing_viking_account or "default",
            pipeline=args.pipeline,
            last_compile_time=args.last_compile_time,
        )
    except ValueError as exc:
        parser.error(str(exc))
    import asyncio

    asyncio.run(service.reconcile(run, key))
    service.run(run, api_key=key, full=args.full)
    print(json.dumps(run.to_public(), ensure_ascii=False, indent=2))
    if run.status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
