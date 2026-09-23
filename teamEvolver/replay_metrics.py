"""Compatibility alias for :mod:`team_replay.metrics`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("team_replay.metrics")
