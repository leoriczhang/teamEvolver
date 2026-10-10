"""Subprocess entry point that wires teamEvolver host services into Replay."""

from __future__ import annotations

from teamEvolver.replay_adapter import ensure_replay_host

ensure_replay_host()

if __name__ == "__main__":
    from team_replay.engine import main

    main()
