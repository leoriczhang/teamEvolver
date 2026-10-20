# ruff: noqa: F401
"""Exercise completion-only mining through submission, remote observation and local output."""

from pathlib import Path

import pytest

from team_miner.skill_miner.knowledge_compiler import AUXILIARY_FILES, SKILL_BODY
from team_miner.skill_miner.knowledge_compiler_v1 import AUXILIARY_FILES as POINT_V1_FILES
from team_miner.skill_miner.knowledge_compiler_v1 import SKILL_BODY as POINT_V1_SKILL
from team_miner.skill_miner.knowledge_compiler_v2 import AUXILIARY_FILES as POINT_V2_FILES
from team_miner.skill_miner.knowledge_compiler_v2 import SKILL_BODY as POINT_V2_SKILL
from team_miner.skill_miner.knowledge_compiler_v3 import AUXILIARY_FILES as POINT_V3_FILES
from team_miner.skill_miner.knowledge_compiler_v3 import SKILL_BODY as POINT_V3_SKILL
from team_miner.skill_miner.knowledge_compiler_v4 import AUXILIARY_FILES as POINT_V4_FILES
from team_miner.skill_miner.knowledge_compiler_v4 import SKILL_BODY as POINT_V4_SKILL
from team_miner.skill_miner.knowledge_compiler_v5 import AUXILIARY_FILES as POINT_V5_FILES
from team_miner.skill_miner.knowledge_compiler_v5 import SKILL_BODY as POINT_V5_SKILL
from team_miner.skill_miner.knowledge_compiler_v6 import AUXILIARY_FILES as POINT_V6_FILES
from team_miner.skill_miner.knowledge_compiler_v6 import SKILL_BODY as POINT_V6_SKILL
from team_miner.skill_miner.knowledge_compiler_v7 import AUXILIARY_FILES as POINT_V7_FILES
from team_miner.skill_miner.knowledge_compiler_v7 import SKILL_BODY as POINT_V7_SKILL
from team_miner.skill_miner.mining_completion import POLICY as COMPLETION_POLICY
from team_miner.skill_miner.mining_completion import completion_only
from team_miner.skill_miner.native_compiler import AUXILIARY_FILES as OLD_FILES
from team_miner.skill_miner.native_compiler import SKILL_BODY as OLD_SKILL
from team_miner.skill_miner.native_compiler import freeze_assets, frozen_assets
from team_miner.skill_miner.settings import compiler_prompt, settings_payload
from team_miner.skill_miner.skill_router_compiler import AUXILIARY_FILES as ROUTER_FILES
from team_miner.skill_miner.skill_router_compiler import SKILL_BODY as ROUTER_SKILL
from team_miner.skill_miner.skill_router_compiler_v3 import AUXILIARY_FILES as ROUTER_V3_FILES
from team_miner.skill_miner.skill_router_compiler_v3 import SKILL_BODY as ROUTER_V3_SKILL
from team_miner.skill_miner.task_compiler import AUXILIARY_FILES as TASK_FILES
from team_miner.skill_miner.task_compiler import SKILL_BODY as TASK_SKILL
from tests.miner.skill_miner.support.native_mining_rounds import new_owner
