# ruff: noqa: F401
"""Short quote identities retain strict assignments and exact original-source proofs."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner import evidence_quote_aliases as aliases
from team_miner.skill_miner import evidence_quote_units as units
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import STAGED_PROTOCOL
from tests.miner.skill_miner.support.deferred_evidence_review import seed
from tests.miner.skill_miner.support.evidence_quote_units import id_response
from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner


def quote_packet(count=18, *, source="viking://resources/refund/中文😀.md", method="item-refund-method-opaque"):
    content = ("Refund “approved” ≤ 50. The literal m001/q001 strings are business text.\n" * 300)[:768 * count]
    original = planning.source_chunks(content, source, max_chars=768 * count)[0]
    original["ordinal"] = 7
    original["source_sha256"] = original["sha256"]
    evidence = [units.source_quote_unit(original, index) for index in range(units.quote_unit_count(original))]
    packet = planning.build_stage_packet(
        units.QUOTE_STAGE, "evidence-review.json", evidence_chunks=evidence,
        controls={"items": [{"item_id": method, "task": "Review q001 and m001 examples faithfully.",
                             "boundary": "Use original refund rules."}],
                  "allowed_source_refs": [source], "quotation_protocol": units.QUOTE_PROTOCOL},
    )
    assert len(evidence) == count
    return packet, original

class QuoteNative(FakeCompile):
    """Inert unchanged public Compile endpoint; only the model response is synthetic."""

    async def request(self, account, method, path, **kwargs):
        if path == "/api/v1/compile" and method == "POST":
            body = kwargs["json"]
            packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
            if packet["stage"] == units.QUOTE_STAGE:
                assert set(body) == {"from", "to", "skill", "instruction"}
                assert all(uri in self.directories for uri in body["from"])
                key = kwargs["extra_headers"]["Idempotency-Key"]
                self.calls.append((key, copy.deepcopy(body)))
                if key not in self.accepted:
                    task_id = "cmp_" + str(len(self.accepted))
                    self.accepted[key] = (task_id, copy.deepcopy(body))
                    self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
                    self.contents[body["to"] + "/evidence-review.json"] = review._json(id_response(packet))
                else:
                    task_id, frozen = self.accepted[key]
                    assert frozen == body
                if self.lose_ack:
                    self.lose_ack = False
                    raise httpx.ReadTimeout("Inert lost acknowledgement after exact-key acceptance")
                return {"task_id": task_id}
        return await super().request(account, method, path, **kwargs)

class QuoteOwner(Owner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = QuoteNative()

async def start_quote_owner(owner, *, aliased=True, lose_ack=False):
    job, _ = owner.new(12)
    job = owner.repo.update_job(job["job_id"], status="running")
    root = owner._staged_root(job)
    state, _, _ = seed(root, [f"Refund procedure {index}: verify receipt, calculate and confirm. "
                             + "x" * 650 for index in range(12)])
    state.pop("evidence_review_protocol")
    if aliased:
        state["quote_alias_protocol"] = aliases.PROTOCOL
    (root / "compiler-base.md").write_text(SKILL_BODY, encoding="utf-8")
    (root / "revision.txt").write_text("", encoding="utf-8")
    state.update(base_sha256=review._sha(SKILL_BODY), round=1, protocol=STAGED_PROTOCOL)
    owner.ov.lose_ack = lose_ack
    job = await owner._staged_evidence_review_next(job, root, state)
    return job, root, state

def frozen_child_bytes(root, current):
    return {key: (root / current[key]).read_bytes() for key in (
        "request_file", "compiler_file", "packet_file", "canonical_packet_file", "quote_alias_file"
    ) if key in current}
