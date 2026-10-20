# ruff: noqa: F401
"""Exercise native submission, incremental input and artifact acceptance through the public host path."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    COMPONENT_PROTOCOL,
    METHOD_SCHEMA,
    PROTOCOL,
    SKILL_BODY,
    compiler_integrity_view,
    frozen_assets,
)
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT, OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_document
from tests.miner.skill_miner.support.ov_round_bridge import SOURCE, FakeCompileClient, FakeOV, Owner


class NativeClient(FakeCompileClient):
    async def install_skill(self, **kwargs):
        result = await super().install_skill(**kwargs)
        if result.get("ok"):
            if not hasattr(self.remote, "compiler_assets"):
                self.remote.compiler_assets = {}
            self.remote.compiler_assets[kwargs["skill_name"]] = kwargs.get("auxiliary_files", {})
        return result

    async def get_skill(self, **kwargs):
        result = await super().get_skill(**kwargs)
        if result.get("ok"):
            files = {"SKILL.md": result["result"]["content"],
                     **getattr(self.remote, "compiler_assets", {}).get(kwargs["skill_name"], {})}
            result["result"]["files"] = [{"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                                         for path, text in files.items()]
        return result

class NativeOwner(Owner):
    def _round_client(self, job):
        return NativeClient(self.remote)

def new_owner(tmp_path, *, rounds=2, mode="single", compiler=SKILL_BODY):
    repo = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
    remote = FakeOV()
    remote.content[SOURCE + "/policy.md"] = "依据原始地区规则核对申请，资料不足时询问并保留结论，禁止猜测资格。"
    for i in range(4):
        remote.content[SOURCE + f"/other-{i}.md"] = f"第{i}份独立背景，不影响地区规则。"
    owner = NativeOwner(repo, remote)
    owner.compiler_body = compiler
    job = repo.create_job(name="原生挖掘验收", account="default", source_uri=SOURCE,
        source_snapshot_uri=RUNS_ROOT + "/pending/source", output_uri=RUNS_ROOT + "/pending/output",
        document_count=5, max_rounds=rounds, skip_empty_checkpoints=True, compiler_body=compiler,
        mining_mode=mode, mining_direction="完整申请核对方法" if mode == "single" else "")
    return owner, repo, remote, job

def complete(remote, job, *, question=False, names=("policy-review",), foreign=False):
    source = job["original_source_snapshot"] + "/original/policy.md"
    refs = ["viking://resources/foreign/file.md"] if foreign else [source]
    for name in names:
        record = {"schema_version": METHOD_SCHEMA, "name": name, "task": "完整核对申请",
                  "trigger": "收到地区申请", "inputs": ["申请与原始地区规则"], "outputs": ["有依据的核对结果"],
                  "boundary": "仅核对申请，不替代真实资格决定", "rationale": "完整且独立的申请核对任务",
                  "source_refs": refs, "questions": [{"qid": "region", "question": "地区适用范围是什么？",
                      "reason": "原文尚未给出地区", "source_refs": refs, "required": True}] if question else []}
        document = build_document(name, [{"query": f"第{i}个地区申请的核对", "name": f"地区核对情境{i}",
            "requirements": [f"核对条件{n}的来源与适用范围" for n in range(12)],
            "trajectory_requirements": ["先阅读原始地区规则再核对申请"], "source_session_ids": refs}
            for i in range(12)])
        document["name"] = "地区申请核对任务集合"
        document["description"] = "验证原始地区规则的完整应用"
        base = job["output_uri"] + f"/compiled_skill/{name}/"
        remote.content.update({
            base + "METHOD.json": json.dumps(record, ensure_ascii=False),
            base + "SKILL.md": (f"---\nname: {name}\ndescription: 依据来源完整核对地区申请。\n"
                                "---\n# 地区申请核对\n\n按原文核对。"),
            base + "EVALUATION.md": "# 地区核对评测\n\n保留条件并核实来源。",
            base + "benchmark.json": json.dumps(document, ensure_ascii=False),
        })
        if job.get("native_component_protocol"):
            prefix = f"analysis/components/{name}"
            reference = f"compiled_skill/{name}/references/conditions.md"
            descriptors = []
            for case in document["cases"]:
                path = f"{prefix}/cases/{case['case_id']}.json"
                descriptors.append({"path": path, "case_id": case["case_id"], "source_refs": refs})
                remote.content[job["output_uri"] + "/" + path] = json.dumps(case, ensure_ascii=False)
            manifest = {"schema_version": job["native_component_protocol"], "name": name,
                "dataset": {key: value for key, value in document.items() if key != "cases"},
                "references": [{"path": reference, "source_refs": refs}], "cases": descriptors,
                "coverage": [{"source_ref": ref, "reference_paths": [reference],
                    "case_ids": [case["case_id"] for case in document["cases"]],
                    "disposition": "included", "reason": "完整核对来源的地区适用条件"} for ref in refs]}
            remote.content[job["output_uri"] + "/" + reference] = "# 地区适用条件\n\n按原始地区规则核对申请。"
            remote.content[job["output_uri"] + "/" + prefix + "/manifest.json"] = json.dumps(
                manifest, ensure_ascii=False,
            )
    remote.tasks[job["ov_task_id"]] = {"status": "completed", "stage": "compile: completed"}
