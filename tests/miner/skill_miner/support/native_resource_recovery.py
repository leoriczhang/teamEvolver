# ruff: noqa: F401
"""Regression for model-selected missing attachments and immutable compiler recovery."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    RESOURCE_POLICY,
    SKILL_BODY,
    request_guidance,
    resource_policy,
)
from team_miner.skill_miner.native_recovery import pending_guidance, prepare_recovery
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner


async def failed_second_round(tmp_path, *, old_guidance="", protocol_failure=False):
    owner, repo, remote, job = new_owner(tmp_path)
    root = repo.jobs_root / job["job_id"]
    legacy = SKILL_BODY.split("\n## 编译 Skill 附件边界\n", 1)[0]
    (root / "compiler-skill.md").write_text(legacy, encoding="utf-8")
    # Reproduce a historical request accepted before the TE resource policy existed.
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=old_guidance):
        first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "review-unresolved", submitted_by="admin")
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=old_guidance):
        second = await owner._submit_ov_skill_round(second)
    error = ("map: schema: 1 validation error for EmitRecords records.1.inputs Field required"
             if protocol_failure else "File not found: " + second["compiler_uri"] + "/references/unknown.md")
    task = {"task_id": second["ov_task_id"], "status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
            "meta": {"pipeline": {"errors": [error]}}}
    remote.tasks[second["ov_task_id"]] = task
    await owner._refresh_ov_skill_job(second)
    return owner, repo, remote, repo.get_job(job["job_id"]), task
