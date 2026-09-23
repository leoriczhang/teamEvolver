"""Compatibility alias for :mod:`team_replay.model_broker`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("team_replay.model_broker")
