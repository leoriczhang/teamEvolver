# ruff: noqa: F401
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_case_batch_orchestration import CASE_BATCH_DIRECTORY_PROTOCOL, CASE_BATCH_STAGE
from team_miner.skill_miner.ov_staged_orchestration import OvStagedOrchestrationMixin, _json, _read, _save, _sha
from tests.miner.skill_miner.fixtures import build_document
from tests.miner.skill_miner.support.host import AdmissionMixin
from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner, drive, start


class BatchFake(FakeCompile):
    def __init__(self):
        super().__init__()
        self.batches = []
        self.break_slots = {}
        self.duplicate = False
        self.same_key_503 = False
        self.deferred_slots = set()
        self.crosspacket = False
        self.extra = False
        self.input_extras = {}

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if path == "/api/v1/fs/ls":
            params = kwargs["params"]
            assert params["output"] == "original" and params["recursive"] == "false"
            assert params["show_all_hidden"] == "false"
            prefix = params["uri"].rstrip("/") + "/"
            entries = {}
            for uri in [*self.contents, *self.input_extras, *self.directories]:
                if not uri.startswith(prefix):
                    continue
                relative = uri[len(prefix):]
                name = relative.split("/", 1)[0]
                is_dir = "/" in relative or uri in self.directories
                if not is_dir and name.startswith("."):
                    continue
                entries[prefix + name] = {"uri": prefix + name, "name": name, "isDir": is_dir}
            rows = sorted(entries.values(), key=lambda row: (not row["isDir"], row["name"].lower(), row["name"]))
            return rows[params["offset"]:params["offset"] + params["node_limit"]]
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/case_batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if any(uri not in self.directories for uri in request["from"]):
            raise HTTPException(422, "Compile source must be a directory")
        assert len(request["from"]) == 1
        if self.same_key_503:
            self.same_key_503 = False
            raise HTTPException(503, "Temporary admission failure")
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_batch_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents)
                       if uri.startswith(prefix) and not uri.rsplit("/", 1)[-1].startswith(".")]
            self.batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}},
            }
            for packet in packets:
                controls = packet["derived_controls"]
                ordinal = int(packet["output_path"].split("-")[-1].split(".")[0])
                failures = self.break_slots.get(ordinal, [])
                failure = failures.pop(0) if failures else ""
                if failure == "missing":
                    continue
                case = build_document(controls["skill_name"], [{"query": "Fixture"}])["cases"][0]
                case["case_id"] = controls["case_id"]
                case["query"] = "针对原文检查此独立边界场景 " + controls["coverage_dimension"] + controls["case_id"]
                if self.duplicate or failure == "duplicate":
                    case["query"] = "所有批内请求完全相同场景"
                case["provenance"]["session_ids"] = controls["allowed_source_refs"]
                if self.crosspacket or failure == "foreign":
                    case["provenance"]["session_ids"] = ["viking://unassigned/foreign.md"]
                if failure == "invalid":
                    case["checks"] = case["checks"][:1]
                if ordinal in self.deferred_slots:
                    case = {
                        "schema_version": planning.DEFERRED_SCHEMA,
                        "reason": "原文缺完整条件",
                        "source_refs": controls["allowed_source_refs"],
                    }
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(case)
            if self.extra:
                self.contents[request["to"] + "/unexpected.json"] = "{}"
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadTimeout("Lost batch ACK after acceptance")
        return {"task_id": task_id}

class BatchOwner(AdmissionMixin, OvStagedOrchestrationMixin):
    """Use the actual production mixin defaults; delegate only inert repository/transport fixtures."""

    def __init__(self, root):
        self.fixture = Owner(root)
        self.fixture.ov = BatchFake()
        self.repo, self.ov = self.fixture.repo, self.fixture.ov

    def __getattr__(self, name):
        return getattr(self.fixture, name)

async def to_batch(owner):
    job = await start(owner, count=12)
    while True:
        _, state = await owner._staged_load(job)
        if state.get("current", {}).get("stage") == CASE_BATCH_STAGE:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])

async def legacy_file_batch(owner, *, accepted=False):
    """Freeze the actual pre-directory protocol without weakening the public gateway fixture."""
    job = await to_batch(owner)
    root, state = await owner._staged_load(job)
    original = copy.deepcopy(state["current"])
    locators = _read(root / original["packet_file"])
    contexts = [row["context"] for row in locators]
    texts = [(root / row["local_path"]).read_text() for row in locators]
    digest = _sha(_json([state["round"], contexts, [_sha(text) for text in texts], original["compiler_sha256"]]))[:24]
    child = CASE_BATCH_STAGE + "-" + digest
    folder = "children/" + child
    input_uri = original["input_uri"].replace(original["child"], child)
    output_uri = original["output_uri"].replace(original["child"], child)
    for row, text in zip(locators, texts, strict=True):
        row["uri"] = row["uri"].replace(original["child"], child)
        row["local_path"] = row["local_path"].replace(original["child"], child)
        path = root / row["local_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    request = _read(root / original["request_file"])
    request.update({"from": [row["uri"] for row in locators], "to": output_uri})
    old = {
        **original, "child": child, "input_uri": input_uri, "output_uri": output_uri,
        "key": original["key"].rsplit("-", 1)[0] + "-" + digest,
        "request_file": folder + "/request.json", "packet_file": folder + "/packets.json",
        "compiler_file": folder + "/compiler.md", "task_id": "",
        "request_sha256": _sha(_json(request)), "packet_sha256": _sha(_json(locators) + "\n"),
    }
    old.pop("input_protocol")
    _save(root / old["packet_file"], locators)
    _save(root / old["request_file"], request)
    (root / old["compiler_file"]).write_bytes((root / original["compiler_file"]).read_bytes())
    _save(root / folder / "metadata.json", old)
    state["current"] = old
    _save(root / "state.json", state)
    owner.ov.accepted.pop(original["key"])
    if accepted:
        owner.ov.accepted[old["key"]] = (original["task_id"], copy.deepcopy(request))
        for path, text in list(owner.ov.contents.items()):
            if path.startswith(original["output_uri"] + "/"):
                owner.ov.contents[path.replace(original["child"], child)] = text
    job = owner.repo.update_job(
        job["job_id"], status="preparing", ov_task_id="", compile_request=request,
        submission_idempotency_key=old["key"],
    )
    return job, root, state
