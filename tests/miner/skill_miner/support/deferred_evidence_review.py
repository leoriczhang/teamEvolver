# ruff: noqa: F401
"""Inert Compile checks for original Evidence review and cross-fragment support."""

from __future__ import annotations

import asyncio
import copy
import re

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy
from team_miner.skill_miner import deferred_evidence_review as review

LANGUAGE = {"language": "zh-CN", "version": 1, "basis": "frozen_original_source"}

def candidate(name="fee-calc"):
    return {
        "name": name,
        "task": "根据数量与费率计算费用",
        "trigger": "收到费用计算请求",
        "inputs": ["数量", "适用费率"],
        "outputs": ["费用"],
        "boundary": "只使用提供的计算规则，调用时缺少参数应请求参数而非虚构数值",
        "rationale": "原文提供可执行计算规则",
    }

def response(packet, outcome="candidate", *, quote=None, reason="原文支持完整费用计算规则"):
    evidence = packet["evidence"]
    quote = quote if quote is not None else evidence[0]["content"][:80]
    quotes = [{"source_uri": evidence[0]["source_uri"], "text": quote}] if quote else []
    return {
        "schema_version": review.REVIEW_SCHEMA,
        "input_ids": packet["input_ids"],
        "reviews": [
            {
                "item_id": packet["derived_controls"]["items"][0]["item_id"],
                "outcome": outcome,
                "reason": reason,
                "source_refs": packet["derived_controls"]["allowed_source_refs"],
                "supporting_quotes": quotes,
                "partial_steps": [{"text": "保留原文中的运算条件", "quote_indexes": [0]}] if quotes else [],
                "candidate": candidate() if outcome == "candidate" else None,
            }
        ],
    }

def seed(tmp_path, texts, *, method_ordinals=None):
    chunks = []
    for index, text in enumerate(texts):
        uri = f"viking://resources/original/document-{index:04d}.md"
        for chunk in planning.source_chunks(text, uri):
            chunk["ordinal"] = len(chunks)
            chunks.append(chunk)
    files = {}
    for chunk in chunks:
        ordinal = chunk["ordinal"]
        review._save(tmp_path / "chunks" / f"{ordinal:08d}.json", chunk)
        metadata = {key: value for key, value in chunk.items() if key != "content"}
        metadata["serialized_chars"] = len(review._json(chunk))
        review._save(tmp_path / "chunk-metadata" / f"{ordinal:08d}.json", metadata)
        files.setdefault(chunk["source_uri"], []).append(ordinal)
    for uri, ordinals in files.items():
        review._save(tmp_path / "source-files" / (review._sha(uri) + ".json"), {"uri": uri, "ordinals": ordinals})
    record = {
        "item": {
            "item_id": "item-method-01",
            "kind": "method",
            "task": "按原文规则计算费用",
            "boundary": "按数量和费率计算，完整规则以原文为准",
            "source_files": list(files),
        },
        "leaf_items": ["item-leaf-01"],
        "ordinals": method_ordinals if method_ordinals is not None else list(range(len(chunks))),
    }
    gap = {"reason": "摘要中未提供费用规则", "source_refs": list(reversed(files))}
    review._save(tmp_path / "proposals" / "00000000.json", record)
    review._save(tmp_path / "method-decisions" / "item-leaf-01.json", {"target": "deferred", **gap})
    review._save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": [gap]})
    (tmp_path / "compiler-base.md").write_text("frozen base", encoding="utf-8")
    state = {
        "phase": review.STAGE,
        "proposal_count": 1,
        "language_policy": LANGUAGE,
        "completed_tasks": 0,
        "estimated_tasks": 1,
        "evidence_review_protocol": "literal-quotes.v1",
    }
    return state, chunks, gap

class Host(review.DeferredEvidenceReviewMixin):
    def __init__(self):
        self.assignments = []
        self.current_packet = None
        self.current = None
        self.compiler = None

    def _staged_stopped(self, job):
        return job.get("status") in {"stopped", "stopping"}

    async def _ensure_aux_compiler(self, root, state, stage, body):
        self.compiler = (stage, body)
        state.setdefault("aux_compilers", {})[stage] = review._sha(body)

    async def _staged_save(self, job, root, state, **changes):
        review._save(root / "state.json", state)
        return {**job, **changes}

    async def _staged_next(self, job, root, state):
        if state["phase"] == review.STAGE:
            return await self._staged_evidence_review_next(job, root, state)
        assert state["evidence_review_complete"]
        assert state["phase"] == "identity_group"
        return job

    async def _staged_submit(self, job, root, state, stage, output_path, chunks, controls, context):
        packet = planning.build_stage_packet(
            stage,
            output_path,
            evidence_chunks=chunks,
            controls=controls,
            language_policy=state["language_policy"],
        )
        assert len(review._json(packet)) <= 32000
        child = "review-" + review._sha(review._json([context, packet]))[:24]
        path = f"children/{child}/packet.json"
        review._save(root / path, packet)
        self.current = {
            "child": child,
            "stage": stage,
            "context": context,
            "packet_file": path,
            "packet_sha256": review._sha(review._json(packet)),
        }
        self.current_packet = packet
        self.assignments.append(
            {
                "mode": controls["review_mode"],
                "ids": packet["input_ids"],
                "source_refs": controls["allowed_source_refs"],
                "size": len(review._json(packet)),
                "scope": {key: controls[key] for key in (
                    "scope_protocol", "review_scope", "routed_scope_chunk_count",
                    "all_original_ranges_reviewed", "all_routed_ranges_reviewed",
                ) if key in controls},
            }
        )
        return job

def run_review(tmp_path, state, responder, monkeypatch):
    monkeypatch.setattr(
        compile_strategy,
        "render_single_stage_compiler",
        lambda base, stage, rules: "---\nname: inert-evidence-review\ndescription: Inert Evidence test\n---\n" + rules,
    )
    monkeypatch.setattr(compile_strategy, "compiler_business_policy", lambda base: {"common": "", "stages": {}})
    host = Host()
    job = {"status": "running"}

    async def run():
        for _ in range(10000):
            await host._staged_evidence_review_next(job, tmp_path, state)
            if state.get("evidence_review_complete"):
                return
            await host._staged_evidence_review_accept(
                job, tmp_path, state, host.current, responder(host.current_packet)
            )
            state["completed_tasks"] += 1
        pytest.fail("Evidence review did not converge")

    asyncio.run(run())
    return host, review._read(tmp_path / "evidence-review/results/00000000.json")

def quote_fixture(text, proposed):
    evidence = planning.source_chunks(text, "viking://resources/original/refund-policy.md")
    items = [{"item_id": "item-refund-01"}]
    packet = {
        "evidence": evidence,
        "input_ids": [chunk["id"] for chunk in evidence],
        "derived_controls": {"items": items, "allowed_source_refs": [evidence[0]["source_uri"]]},
    }
    return evidence, items, response(packet, quote=proposed)
