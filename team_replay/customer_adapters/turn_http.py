"""Owner-configured turn endpoint. Isolation must be guaranteed by the runtime."""
import os
from team_replay.factories import TurnBasedReplayFactory

REPLAY_ADAPTER = {"label": "HTTP turn adapter", "enabled": True}


def build_replay_adapter(config):
    return TurnBasedReplayFactory(
        endpoint=os.environ.get("REPLAY_TURN_URL", ""), runtime_type="customer",
        api_key=os.environ.get("REPLAY_TURN_API_KEY", ""),
        isolated_sessions=os.environ.get("REPLAY_TURN_ISOLATED", "") == "1",
    )
