"""DreamCycle entry point.

Usage:
    dreamcycle              # Single round (default)
    dreamcycle --once       # Single round (explicit)
    dreamcycle --daemon     # Continuous daemon (active during 0:00-6:00)
    dreamcycle --status     # Show state & history
    dreamcycle --dry-run    # Plan only, no execution
    dreamcycle --reset      # Clear all local state (state.json, logs, reports)
    dreamcycle --reset --remote  # Clear local state + remote team space
    dreamcycle --reset --dry-run # Show what would be cleared
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        prog="dreamcycle",
        description="DreamCycle — Autonomous team knowledge maintenance agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  dreamcycle --once              Run one maintenance round now
  dreamcycle --daemon            Run as overnight daemon (0:00-6:00)
  dreamcycle --status            Show execution history
  dreamcycle --once --jobs team_overview,dedup  Run specific jobs only
""",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", default=True,
                      help="Run a single round immediately (default)")
    mode.add_argument("--daemon", action="store_true",
                      help="Run as continuous daemon")
    mode.add_argument("--status", action="store_true",
                      help="Show current state and recent history")
    mode.add_argument("--reset", action="store_true",
                      help="Clear all local state (state.json, logs, reports)")

    parser.add_argument("--env-file", type=str, default=None,
                        help="Path to .env file (default: .env or ~/.dreamcycle/.env)")
    parser.add_argument("--jobs", type=str, default=None,
                        help="Comma-separated list of jobs to run (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without executing")
    parser.add_argument("--remote", action="store_true",
                        help="With --reset: also clear remote OpenViking team space")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    # Load config
    env_file = args.env_file
    if not env_file:
        candidates = [".env", str(Path("~/.dreamcycle/.env").expanduser())]
        for c in candidates:
            if Path(c).exists():
                env_file = c
                break

    from .config import DreamCycleConfig
    config = DreamCycleConfig(env_file=env_file)
    from ..observability import configure_langfuse

    configure_langfuse()

    if args.verbose:
        config.log.log_level = "DEBUG"

    # Handle --status (no file logging needed)
    if args.status:
        _show_status(config)
        return

    # Handle --reset
    if args.reset:
        from .reset import reset
        # --dry-run with --reset: show what would be cleared
        dry_run = args.dry_run
        remote = args.remote
        output = reset(config, remote=remote, dry_run=dry_run)
        print(output)
        return

    # Select jobs
    from .jobs import ALL_JOBS
    if args.jobs:
        job_names = set(args.jobs.split(","))
        selected_jobs = [j for j in ALL_JOBS if j().name in job_names]
        if not selected_jobs:
            print(f"Error: no matching jobs for: {args.jobs}")
            print(f"Available: {', '.join(j().name for j in ALL_JOBS)}")
            sys.exit(1)
    else:
        selected_jobs = ALL_JOBS

    # Handle --dry-run (no file logging needed)
    if args.dry_run:
        _show_dry_run(selected_jobs)
        return

    # Setup logging (with file handlers) — only for actual execution
    from .logging_config import setup_logging
    setup_logging(config.log)

    # Run
    from .scheduler import Scheduler
    scheduler = Scheduler(config, selected_jobs)

    logger.info("DreamCycle v0.1.0 starting")
    logger.info("  Mode: %s", "daemon" if args.daemon else "once")
    logger.info("  Jobs: %s", ", ".join(j().name for j in selected_jobs))
    logger.info("  Window: %02d:00-%02d:00",
                config.scheduler.active_start_hour, config.scheduler.active_end_hour)
    logger.info("  Rounds/window: %d (every %dm)",
                config.scheduler.rounds_per_window, config.scheduler.round_interval_minutes)
    logger.info("  LLM: %s @ %s", config.llm.model, config.llm.base_url[:40])
    logger.info(
        "  Viking: %s (agent_id=%s, customer_id=%s)",
        config.viking.endpoint,
        config.viking.agent_id,
        config.viking.customer_id or "(agent-level)",
    )

    try:
        if args.daemon:
            scheduler.run_daemon()
        else:
            results = scheduler.run_once()
            # Exit code: 0 if all ok, 1 if any job failed
            has_failure = any(r.status.value == "failed" for r in results)
            sys.exit(1 if has_failure else 0)
    finally:
        from ..observability import flush_langfuse

        flush_langfuse()


def _show_status(config):
    """Display current state and history."""
    from .scheduler import ExecutionState
    state = ExecutionState(config.log.state_file)

    print("\n╭─── DreamCycle Status ───────────────────────╮")
    print(f"│  Last run: {state.last_run_date or 'never'}")
    print(f"│  Total cycles: {state.total_cycles}")
    print(f"│  Rounds today: {state.rounds_today}")
    print(f"│  State file: {config.log.state_file}")
    print(f"│  Log dir: {config.log.log_dir}")
    print(f"│  Report dir: {config.log.report_dir}")
    print("╰─────────────────────────────────────────────╯\n")

    # Show recent history
    if config.log.state_file.exists():
        data = json.loads(config.log.state_file.read_text())
        history = data.get("history", [])
        if history:
            print("Recent rounds:")
            for entry in history[-10:]:
                date = entry.get("date", "?")
                round_num = entry.get("round", "?")
                jobs = entry.get("jobs", [])
                completed = sum(1 for j in jobs if j.get("status") == "completed")
                failed = sum(1 for j in jobs if j.get("status") == "failed")
                duration = sum(j.get("duration_seconds", 0) for j in jobs)
                print(f"  {date} R{round_num}: {completed}✅ {failed}❌ ({duration:.0f}s)")
        else:
            print("  (no history)")
    print()


def _show_dry_run(job_classes):
    """Show what would be executed without actually running."""
    print("\n╭─── DreamCycle Dry Run ──────────────────────╮")
    print("│  The following jobs would execute:           │")
    print("╰─────────────────────────────────────────────╯\n")

    for job_class in sorted(job_classes, key=lambda j: j().priority):
        job = job_class()
        plan = job.create_plan()
        print(f"  📋 [{job.priority:02d}] {job.name}: {job.description}")
        for task in plan.tasks:
            print(f"       ⬜ {task.description}")
        print()

    print(f"  Total: {len(job_classes)} jobs")
    print()


if __name__ == "__main__":
    main()
