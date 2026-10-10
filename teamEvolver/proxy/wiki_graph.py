"""Compatibility entry point for the mining module."""
import sys
from team_miner import wiki_graph as _module
sys.modules[__name__] = _module
