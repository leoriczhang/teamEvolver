"""Run a real, unpublished MA Skill A/B acceptance through the API and Worker.

Use the deployment's environment (database and Root Key or console login).
The receipt contains IDs and hashes only; credentials and full configs stay off disk.
This tests storyboard production, not full video rendering or publish eligibility.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

from scripts.connect_managed_agent import Console
from team_ma.repository import MARepository
from team_replay.datasets.schema import make_document
from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.history import skill_payload
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.bootstrap import load_bootstrap
from teamEvolver.database.pool import DatabasePool
from teamEvolver.database.transaction import TransactionManager


async def start(args, console):
    config = load_bootstrap()
    pool = DatabasePool(dsn=config.storage_pg_dsn, schema=config.storage_pg_schema, pool_min=1, pool_max=2)
    tx = TransactionManager(pool)
    try:
        async with tx.tenant_tx(args.tenant) as conn:
            connection = await MARepository(tx).connection(conn, args.connection)
            applied = await MARepository(tx).applied(conn, args.connection)
            candidate = await CandidateRepository(tx).require(conn, args.candidate)
            proposed = await CandidateRepository(tx).read_bundle(conn, args.candidate)
            baseline = await SkillLibraryRepository(tx).read_version_bundle(
                conn, candidate["skill_name"], candidate["base_version"],
            )
        if not connection or not connection["enabled"] or not applied:
            raise ValueError("An active, fully applied MA execution target is required")
        source = applied["record"]
        runtime = {
            "type": "managed-agents", "connection_id": args.connection,
            "connection_revision": connection["revision"],
            "agent_graph": source["agent_graph"], "environment": source["environment"],
        }
        brief = json.dumps({
            "topic": "团队技能如何沉淀", "duration_seconds": 6,
            "format": "16:9", "shots": 3, "tone": "清晰简洁", "approved": True,
        }, ensure_ascii=False).encode()
        query = (
            "请加载 codingvideo-production-core，准备一个已批准创意的 6 秒视频草稿分镜计划。"
            "读取提供的 brief.json，只做分镜计划，暂不渲染视频或访问网络。"
            "请直接用文件工具把 JSON 写到 /mnt/session/outputs/storyboard.json，"
            "包含 topic、duration_seconds、format、shots；shots 必须有三个镜头，"
            "每个镜头有 start_seconds、end_seconds、caption，总时长为 6 秒。"
            "无需再确认创意，完成后告知文件路径。"
        )
        case = {
            "case_id": "storyboard-6s", "query": query, "skill_ids": [candidate["skill_name"]],
            "checks": [
                {"id": "storyboard", "kind": "output",
                 "text": ("实际生成可读取的 storyboard.json，包含主题、6秒总时长、16:9格式和三个镜头；"
                          "镜头含起止秒与字幕，覆盖0到6秒。")},
                {"id": "brief", "kind": "trajectory",
                 "text": ("通过实际工具读取提供的 brief.json，并使用其中的主题“团队技能如何沉淀”；"
                          "只准备分镜计划，无需渲染视频。")},
            ],
            "materials": [{"path": "brief.json", "size": len(brief), "sha256": hashlib.sha256(brief).hexdigest(),
                           "content_b64": base64.b64encode(brief).decode()}],
            "provenance": {"source_runtime": runtime, "acceptance_scope": "storyboard-production",
                           "source_release_id": applied["release_id"]},
        }
        dataset = make_document(
            dataset_id=args.key, name="MA 同 Agent 同环境 · 分镜回放验收",
            skills=[candidate["skill_name"]], cases=[case], source={"kind": "manual"},
        )
        existing = console.request("GET", "/api/datasets/" + args.key, optional=True)
        if not existing:
            console.request("POST", "/api/datasets", json={
                **dataset, "files": [{"path": "brief.json", "content_b64": base64.b64encode(brief).decode()}],
            })
        payload = {
            "subject_id": candidate["skill_name"], "dataset": dataset, "runtime": runtime,
            "baseline": {"revision": candidate["base_version"],
                         "payload": skill_payload(candidate["skill_name"], baseline)},
            "candidate": {"revision": candidate["revision"],
                          "payload": skill_payload(candidate["skill_name"], proposed)},
            "options": {"timeout_seconds": 900, "max_interactions": 4, "concurrency": 1},
        }
        receipt = console.request("POST", "/api/replays/skill", json=payload,
                                  headers={"Idempotency-Key": args.key})
        receipt.update(
            tenant_id=args.tenant, candidate_id=args.candidate, candidate_revision=candidate["revision"],
            baseline_version=candidate["base_version"], source_release_id=applied["release_id"],
            scope="storyboard-production", published=False,
        )
        Path(args.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps(receipt, ensure_ascii=False))
    finally:
        await pool.aclose()
        pool.close()


def check(args, console):
    receipt = json.loads(Path(args.receipt).read_text())
    run_id = receipt.get("run_id") or receipt["job_id"]
    job = console.request("GET", "/api/jobs/" + run_id)
    detail = console.request("GET", "/api/replays/skill/" + run_id)
    result = detail.get("result") or {}
    pairs = result.get("cases") or []
    summary = {
        "run_id": run_id, "job_status": job["status"], "verdict": result.get("verdict"),
        "reason": result.get("reason"),
        "branches": {
            branch: {key: (pairs[0].get(branch) or {}).get(key) for key in
                     ("status", "error", "completed", "interaction_turns", "total_tokens", "tool_call_count",
                      "execution_snapshot", "checklist_report", "final_response")}
            for branch in ("baseline", "candidate")
        } if pairs else {},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.action == "check":
        branches = summary["branches"]
        assert job["status"] == "succeeded", "Worker has not succeeded"
        assert len(branches) == 2 and all(b["completed"] for b in branches.values()), "Both branches must complete"
        a, b = [branches[branch]["execution_snapshot"] for branch in ("baseline", "candidate")]
        assert a["session_id"] != b["session_id"]
        for key in ("agent_id", "agent_version", "environment_id", "environment_config_sha256",
                    "runtime_tree_sha256", "materials"):
            assert a[key] == b[key], f"Uncontrolled difference: {key}"
        assert a["skills"] != b["skills"] and a["agent_override_fields"] == b["agent_override_fields"] == ["skills"]
        assert a["runtime_tree_sha256"], "Real Runtime proof is required"
        assert all(isinstance(branch["total_tokens"], int) for branch in branches.values())
        assert all(any(a.get("path") == "storyboard.json" for a in pairs[0][branch].get("artifacts", []))
                   for branch in branches), "Both actual storyboard files must be archived"
        evidence = {**receipt, **summary, "passed": True}
        Path(args.receipt).with_suffix(".evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2))
        print("PASS: real MA A/B completed with identical pinned Agent, Environment, Runtime and materials")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "check"])
    parser.add_argument("--url", default="http://127.0.0.1:52010")
    parser.add_argument("--tenant", default="codingvideo-ma")
    parser.add_argument("--connection", default="codingvideo-visual")
    parser.add_argument("--candidate", default="20261102101424-codingvideo-production-core-e3114deb")
    parser.add_argument("--key", default="ma-skill-only-storyboard-20261113")
    parser.add_argument("--receipt", default="runtime/ma-replay-acceptance.json")
    args = parser.parse_args()
    console = Console(args.url)
    console.login()
    console.tenant = args.tenant
    try:
        if args.action == "start":
            asyncio.run(start(args, console))
        else:
            check(args, console)
    finally:
        console.client.close()


if __name__ == "__main__":
    main()
