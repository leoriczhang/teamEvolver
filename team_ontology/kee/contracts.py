"""Small versioned structures. Capability bindings describe use; they do not authorize execution."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(Contract):
    source_id: str
    uri: str = ""
    revision: str
    digest: str
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=10000)


class CapabilityBinding(Contract):
    contract: Literal["kee.capability-binding.v1"] = "kee.capability-binding.v1"
    skill_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    description: str = Field(min_length=1, max_length=4000)


class CaseEvidence(Contract):
    contract: Literal["kee.case-evidence.v1"] = "kee.case-evidence.v1"
    kind: Literal["Evidence", "Session", "receipt"]
    reference: str = Field(min_length=1, max_length=2000)
    digest: str = ""
    outcome: Literal["supported", "contradicted", "unknown"] = "unknown"
    # Deliberately excludes complete Session traces, bodies, user lists and secrets.


class Method(Contract):
    method_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(min_length=1, max_length=4000)
    conditions: list[str] = Field(default_factory=list, max_length=100)
    prohibitions: list[str] = Field(default_factory=list, max_length=100)
    exceptions: list[str] = Field(default_factory=list, max_length=100)
    steps: list[str] = Field(min_length=1, max_length=100)
    completion: list[str] = Field(min_length=1, max_length=100)
    evidence: list[SourceRef] = Field(min_length=1, max_length=100)
    capabilities: list[CapabilityBinding] = Field(default_factory=list, max_length=50)
    cases: list[CaseEvidence] = Field(default_factory=list, max_length=100)
    related_methods: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("conditions", "prohibitions", "exceptions", "steps", "completion")
    @classmethod
    def bounded_items(cls, value):
        if any(not v.strip() or len(v) > 4000 for v in value):
            raise ValueError("EMPTY_OR_OVERSIZED_METHOD_ITEM")
        return value


class Fact(Contract):
    fact_id: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    value: str | int | bool
    polarity: Literal["positive", "negative"] = "positive"
    evidence: list[SourceRef] = Field(min_length=1, max_length=100)


class ProcedurePack(Contract):
    contract: Literal["kee.procedure-pack.v1"] = "kee.procedure-pack.v1"
    pack_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    version: str = Field(min_length=1, max_length=100)
    schema_revision: str = Field(min_length=1, max_length=100)
    schema_body: dict = Field(default_factory=dict)
    methods: list[Method] = Field(min_length=1, max_length=10000)
    facts: list[Fact] = Field(default_factory=list, max_length=10000)
    gaps: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def references(self):
        ids = [m.method_id for m in self.methods]
        if len(set(ids)) != len(ids):
            raise ValueError("DUPLICATE_METHOD_ID")
        for method in self.methods:
            if not set(method.related_methods) <= set(ids):
                raise ValueError("UNKNOWN_RELATED_METHOD")
        return self


class WikiRelease(Contract):
    contract: Literal["kee.wiki-release.v1"] = "kee.wiki-release.v1"
    pack_digest: str
    target_uri: str
    baseline: str = ""
    pages: dict[str, str]  # relative path -> UTF-8 SHA-256
    sources: list[str]
    approval_id: str
    state: Literal[
        "private",
        "uploading",
        "content_verified",
        "index_wait",
        "entry_unknown",
        "published",
        "withdraw_unknown",
        "withdrawn",
    ] = "private"
    processing_task_ids: list[str] = Field(default_factory=list)
    receipt: dict = Field(default_factory=dict)


def verify_evidence(pack: ProcedurePack, sources: list[dict]):
    lookup = {s["source_id"]: s for s in sources}
    for method in [*pack.methods, *pack.facts]:
        for ref in method.evidence:
            source = lookup.get(ref.source_id)
            if not source:
                raise ValueError("SOURCE_REFERENCE_NOT_FOUND")
            text = source["text"]
            if (
                ref.revision != source["revision"]
                or ref.digest != hashlib.sha256(text.encode()).hexdigest()
                or not 0 <= ref.start < ref.end <= len(text)
                or text[ref.start : ref.end] != ref.quote
                or ref.uri != source.get("uri", "")
            ):
                raise ValueError("SOURCE_EVIDENCE_MISMATCH")
    return True


def safe_uri(uri: str):
    # Generic resources only. Reject encoded traversal and links outside the selected target.
    from urllib.parse import unquote, urlsplit

    p = urlsplit(uri.rstrip("/"))
    parts = unquote(p.path).split("/")[1:]
    if p.scheme != "viking" or p.netloc != "resources" or p.query or p.fragment or not parts:
        raise ValueError("RESOURCE_URI_REQUIRED")
    if any(not x or x in {".", ".."} or re.search(r"[\\\x00-\x1f]", x) for x in parts):
        raise ValueError("UNSAFE_RESOURCE_URI")
    return uri.rstrip("/")
