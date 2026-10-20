# ruff: noqa: F401
"""Real file responses, scope isolation, and original rules in Skill-owned preparation."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.miner.skill_miner.support import COMPILER_ROOT
from tests.miner.skill_miner.support.knowledge_skill_preparation import Workspace, identity, put

SCRIPT = COMPILER_ROOT / "compiler_skills/knowledge-point-v7/scripts/prepare_point.py"

PREP = None

@pytest.fixture(params=["v7", "v8"], autouse=True)
def preparation_version(request, monkeypatch):
    script = SCRIPT.parents[2] / ("knowledge-point-" + request.param) / "scripts/prepare_point.py"
    spec = importlib.util.spec_from_file_location("collection_" + request.param, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(__import__(__name__, fromlist=["PREP"]), "PREP", module)

def plan(workspace, points, old=()):
    ids = workspace.indexes + list(old)
    folder = workspace.drafts / "planning"
    put(folder / "assignment.json", {"inputs": [{"id": rid} for rid in ids]})
    result = PREP.run(workspace.root, {"mode": "plan_collection", "anchor_input_id": ids[0], "points": points})
    response = json.loads((folder / result["record_response_ref"]).read_text())
    record = response["records"][0]
    rid = identity("selected-collection")
    refs = []
    for parent in record["inputs"]:
        refs.extend(json.loads((workspace.base / "records" / (parent + ".json")).read_text())["source_refs"])
    put(workspace.base / "records" / (rid + ".json"),
        {"parents": record["inputs"], "source_refs": list(dict.fromkeys(refs)), "payload_ref": "payloads/" + rid})
    put(workspace.base / "payloads" / (rid + ".json"), {"fields": record["payload"]})
    writer = workspace.drafts / "collection-writer"
    put(writer / "assignment.json", {"inputs": [{"id": rid}]})
    return rid, writer, record

def add_old(workspace, kid, text, relative="SKILL.md"):
    uri = "viking://resources/test/source/revision/compiled_skill/knowledge-" + kid + "/" + relative
    ids = workspace.add_source(uri, text)
    rid = identity("old-" + kid + ":" + relative)
    put(workspace.base / "records" / (rid + ".json"),
        {"parents": ids, "source_refs": ids, "payload_ref": "payloads/" + rid})
    put(workspace.base / "payloads" / (rid + ".json"),
        {"fields": {"kind": "context-locator", "uri": uri, "is_factual": False}})
    return rid
