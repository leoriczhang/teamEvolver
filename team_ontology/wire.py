"""Versioned wire types shared through JSON Schema, not cross-repository imports."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT = "sf.ontology.v1"


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Wire):
    source_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)
    text: str = Field(max_length=2_000_000)
    readers: list[str] = Field(min_length=1, max_length=1000)
    provenance: Literal["document", "trusted_connector"] = "document"
    origin_uri: str = Field(default="", max_length=2000)


class Predicate(Wire):
    subject_type: str
    value_type: Literal["string", "number", "boolean", "entity"] = "string"
    object_type: str | None = None
    required_qualifiers: list[str] = []


class Rule(Wire):
    rule_id: str
    predicate: str = ""
    equals: str | int | float | bool = ""
    required_qualifiers: dict[str, str] = {}
    expression: dict | None = None


class ToolContract(Wire):
    tool_id: str
    revision: str
    evidence_slots: list[str]
    input_schema: dict
    read_only: Literal[True] = True


class Schema(Wire):
    revision: str
    entity_types: list[str] = Field(min_length=1, max_length=100)
    predicates: dict[str, Predicate]
    rules: list[Rule] = Field(default_factory=list, max_length=100)
    issue_pack_revision: str
    evidence_slots: list[str] = Field(default_factory=list, max_length=100)
    tools: list[ToolContract] = Field(default_factory=list, max_length=100)


class SourceRef(Wire):
    source_id: str
    revision: str
    digest: str


class Manifest(Wire):
    contract: Literal["sf.ontology.v1"] = CONTRACT
    profile: Literal["sf-ontology-build.v1"] = "sf-ontology-build.v1"
    schema_revision: str
    sources: list[SourceRef] = Field(min_length=1, max_length=1000)
    expected_generation: int = Field(ge=0)
    mode: Literal["delta", "replace"] = "delta"
    extractor: Literal["fixture", "llm"] = "llm"
    deadline_seconds: int = Field(default=120, ge=10, le=600)
    max_assertions: int = Field(default=200, ge=1, le=10000)


class Entity(Wire):
    entity_id: str
    authority_namespace: str
    entity_type: str
    external_id: str
    label: str
    aliases: list[str] = Field(default_factory=list, max_length=20)


class Evidence(Wire):
    evidence_id: str
    source_id: str
    revision: str
    digest: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)


class Assertion(Wire):
    assertion_id: str
    subject: str
    predicate: str
    value: str | int | float | bool
    polarity: Literal["positive", "negative"] = "positive"
    qualifiers: dict[str, str] = {}
    epistemic_kind: Literal["document_fact", "user_claim", "system_fact", "derived"] = (
        "document_fact"
    )
    valid_from: datetime
    valid_to: datetime | None = None
    # One proof is an AND of evidence IDs. Multiple independently checked proofs are OR.
    support_sets: list[list[str]] = Field(min_length=1, max_length=20)
    premises: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def interval(self):
        if self.valid_from.tzinfo is None or (self.valid_to and self.valid_to.tzinfo is None):
            raise ValueError("timezone required")
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("empty validity interval")
        return self


class CandidateBundle(Wire):
    contract: Literal["sf.ontology.v1"] = CONTRACT
    manifest_digest: str
    entities: list[Entity] = Field(max_length=10000)
    assertions: list[Assertion] = Field(max_length=10000)
    evidence: list[Evidence] = Field(max_length=20000)
    tombstones: list[str] = Field(default_factory=list, max_length=10000)
    schema_proposals: list[dict] = Field(default_factory=list, max_length=100)
    extraction: dict[str, Any] = {}


class Submission(Wire):
    submission_key: str = Field(min_length=1, max_length=200)
    manifest_ref: str
    manifest_digest: str


class Prepare(Wire):
    task_id: str
    candidate_digest: str


class Approval(Wire):
    prepared_id: str
    note: str = Field(min_length=1, max_length=2000)


class Commit(Wire):
    prepared_id: str
    commit_key: str = Field(min_length=1, max_length=200)
    grant: str


class TypedQuery(Wire):
    entity_ids: list[str] = Field(default_factory=list, max_length=50)
    predicates: list[str] = Field(default_factory=list, max_length=100)
    valid_at: datetime | None = None
    known_at: datetime | None = None
    max_hops: int = Field(default=1, ge=1, le=2)
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_assertions: int = Field(default=200, ge=1, le=200)
    max_examined_edges: int = Field(default=5000, ge=1, le=5000)

    @model_validator(mode="after")
    def timezone_required(self):
        if any(t is not None and t.tzinfo is None for t in (self.valid_at, self.known_at)):
            raise ValueError("timezone required")
        return self


class ContextRequest(TypedQuery):
    task_ref: str = Field(default="", max_length=200)
    token_budget: int = Field(default=4000, ge=64, le=16000)
    claim: str = Field(default="", max_length=4000)
    observation_handles: list[str] = Field(default_factory=list, max_length=20)


class Observation(Wire):
    task_ref: str
    subject: str
    predicate: str
    value: str | int | float | bool
    source_id: str
    revision: str
    ttl_seconds: int = Field(default=60, ge=1, le=300)
    audience: str


class ContextPacket(Wire):
    contract: Literal["sf.ontology.context.v1"] = "sf.ontology.context.v1"
    semantic_generation: int
    schema_revision: str | None
    facts: list[dict]
    observations: list[dict]
    user_claim: str
    conflicts: list[dict]
    missing_evidence: list[str]
    evaluations: list[dict]
    next_queries: list[dict] = []
    truncated: bool
    degraded: bool = False
    prompt_fragment: str
    authorization_epoch: int


class PublicationGrant(Wire):
    prepared_id: str
    tenant: str
    reviewer: str
    candidate_digest: str
    manifest_digest: str
    expected_generation: int
    epoch: int
    expires_at: int
    nonce: str


class ErrorResponse(Wire):
    contract: Literal["sf.ontology.error.v1"] = "sf.ontology.error.v1"
    detail: str
    request_id: str
    retryable: bool
    details: list[dict] = []


class PublicationApproval(Wire):
    prepared_id: str
    tenant: str
    reviewer: str
    note: str = Field(min_length=1, max_length=2000)
    candidate_digest: str
    manifest_digest: str
    expected_generation: int = Field(ge=0)
    epoch: int = Field(ge=0)
    expires_at: int
    approval_id: str = Field(min_length=1, max_length=200)


class CommitV2(Wire):
    contract: Literal["sf.ontology.commit.v2"]
    prepared_id: str
    commit_key: str = Field(min_length=1, max_length=200)
    approval: PublicationApproval
