"""Compatibility entry point for the mining module."""
import sys
from team_miner import routes as _module
sys.modules[__name__] = _module
