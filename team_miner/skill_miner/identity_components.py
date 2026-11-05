"""Retain original business contracts when identities consolidate, without treating them as Evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

PROTOCOL = "te-candidate-consolidation.v1"


def digest(members: list[dict]) -> str:
    return hashlib.sha256(json.dumps(members, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def members_for(rows: list[dict]) -> list[dict]:
    members = {}
    for row in rows:
        parts = row.get("consolidated_members") or [{
            "candidate_id": row["candidate_id"], "candidate": row["candidate"],
            "scope": row["scope"], "prior": row["prior"],
        }]
        for part in parts:
            identity = part["candidate_id"]
            if identity in members and members[identity] != part:
                raise ValueError("Consolidated member business contract changed")
            members[identity] = copy.deepcopy(part)
    return [members[key] for key in sorted(members)]


def controls(root: Path, name: str) -> dict:
    path = root / "identity" / "components" / (name + ".json")
    if not path.exists():
        return {}
    from .ov_staged_orchestration import _read

    record = _read(path)
    if (record.get("protocol") != PROTOCOL or record.get("name") != name
            or record.get("members_sha256") != digest(record["members"])):
        raise ValueError("Frozen consolidated member contracts changed")
    return {
        "consolidation_protocol": PROTOCOL,
        "component_contracts": [row["candidate"] for row in record["members"]],
        "component_contracts_are_evidence": False,
        "component_rule": "Keep one complete parent method; preserve every source-supported member condition "
        "and branch at its owning step. Components are identity context, not factual Evidence. "
        "Do not concatenate duplicate workflows, discard a refinement or invent an independent child goal.",
    }
