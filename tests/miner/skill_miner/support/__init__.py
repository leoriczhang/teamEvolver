"""Shared domain fixtures; test modules never import other test modules."""

from pathlib import Path

import team_miner.skill_miner

COMPILER_ROOT = Path(team_miner.skill_miner.__file__).parent
