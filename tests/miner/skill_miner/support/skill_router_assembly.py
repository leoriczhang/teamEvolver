# ruff: noqa: F401
"""Router assembly preserves supplied metadata without promoting it into common rules."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.miner.skill_miner.support import COMPILER_ROOT
from tests.miner.skill_miner.support.knowledge_skill_preparation import identity, put

SCRIPT = COMPILER_ROOT / "compiler_skills/skill-router-v4/scripts/assemble_router.py"

SPEC = importlib.util.spec_from_file_location("router_assembly_v4", SCRIPT)

ASSEMBLE = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(ASSEMBLE)

def directory(root, roots=None):
    base = root / "__compile_staging__/pipeline"
    ids = []
    for index in [1, 2]:
        rid, sid = identity("entry" + str(index)), identity("range" + str(index))
        ids.append(rid)
        slug = "method-" + str(index)
        entry = {"slug": slug, "name": "方法" + str(index),
                 "dependency_root": (roots or ["viking://resources/source/library"] * 2)[index - 1],
                 "skill_path": slug + "/SKILL.md", "config_path": slug + "/references/config.json",
                 "gaps": ["原文尚未确认的条件"] if index == 1 else [],
                 "tasks": [{"id": "1", "title": "原任务"}],
                 "scenes": [{"id": "1", "mode": "本场景连续三轮才触发；不是通用要求"}]}
        put(base / "records" / (rid + ".json"), {"payload_ref": "payloads/" + rid, "source_refs": [sid]})
        put(base / "payloads" / (rid + ".json"), {"fields": {"kind": "skill-entry", "entry": entry}})
        put(base / "sources" / (sid + ".json"), {"uri": "viking://resources/test/source/original/" + slug + ".md"})
    caller = root / "__compile_staging__/drafts/writer"
    put(caller / "assignment.json", {"inputs": [{"id": rid} for rid in ids]})
    return caller, base, ids
