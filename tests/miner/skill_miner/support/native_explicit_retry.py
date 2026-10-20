# ruff: noqa: F401
"""An explicit administrator action authorizes one independent budget, never implicit renewal."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    record_format_guidance,
    request_guidance,
    retry_review_guidance,
)
from team_miner.skill_miner.native_recovery import (
    effective_round_budget,
    ensure_round_budget,
    prepare_explicit_retry,
)
from tests.miner.skill_miner.support.native_mining_rounds import complete
from tests.miner.skill_miner.support.native_resource_recovery import failed_second_round


def authorization(job):
    return {"action": "retry_with_new_budget", "request_id": str(uuid.uuid4()),
            "expected_task_id": job["ov_task_id"], "budget_seconds": job["runtime_timeout_seconds"],
            "authorized_by": "reviewer"}

def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name not in {"job.json", "run.log"}}
