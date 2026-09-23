"""Deterministic, model-independent conversion of Compile drafts to ontology candidates."""

import copy
from typing import Literal
from urllib.parse import unquote, urlsplit

import yaml
from fastapi import HTTPException
from pydantic import Field, field_validator

from .wire import Assertion, CandidateBundle, Entity, Evidence, Schema, SourceRef, Wire, digest

DEFAULT_COMPILE_SKILL_URI = "viking://agent/skills/ontology-extraction-v1"


def normalize_skill_uri(value):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    parts = unquote(parsed.path).strip("/").split("/")
    if parts[-1] == "SKILL.md":
        parts.pop()
    valid = (parsed.netloc == "agent" and len(parts) == 2 and parts[0] == "skills") or (
        parsed.netloc == "user" and len(parts) == 3 and parts[1] == "skills"
    )
    if (
        parsed.scheme != "viking"
        or parsed.query
        or parsed.fragment
        or not valid
        or any(not p or p in {".", ".."} or any(c in p for c in "%\\\x00") for p in parts)
    ):
        raise ValueError("COMPILE_SKILL_URI_INVALID")
    return "viking://" + parsed.netloc + "/" + "/".join(parts)


def compile_skill_digest(text):
    """OV serializes YAML front matter; line wrapping is not a Skill revision."""
    sections = text.replace("\r\n", "\n").split("---", 2)
    if len(sections) != 3 or sections[0].strip():
        raise ValueError("COMPILE_SKILL_FORMAT_INVALID")
    metadata = yaml.safe_load(sections[1])
    if not isinstance(metadata, dict):
        raise ValueError("COMPILE_SKILL_FORMAT_INVALID")
    return digest({"metadata": metadata, "body": sections[2].strip()})


SOURCE_CONTRACT = "sf.te.ontology.sources.v1"
BUILD_CONTRACT = "sf.te.ontology.compile.v1"
OUTPUT_CONTRACT = "sf.te.ontology.drafts.v1"
INPUT_SCHEMA = Schema(
    revision="te-compile-input-v1", entity_types=["Document"], predicates={}, issue_pack_revision="te-compile-input-v1"
)


class SourceCollectionRequest(Wire):
    contract: Literal["sf.te.ontology.sources.v1"] = SOURCE_CONTRACT
    submission_key: str = Field(min_length=1, max_length=200)
    roots: list[str] = Field(default_factory=list, max_length=20)
    references: list[SourceRef] = Field(default_factory=list, max_length=1000)
    readers: list[str] = Field(min_length=1, max_length=1000)


class CompileBuildRequest(Wire):
    contract: Literal["sf.te.ontology.compile.v1"] = BUILD_CONTRACT
    submission_key: str = Field(min_length=1, max_length=200)
    collection_id: str
    expected_generation: int = Field(ge=0)
    skill_uri: str = Field(default="", max_length=2000)

    @field_validator("skill_uri")
    @classmethod
    def validate_skill_uri(cls, value):
        return normalize_skill_uri(value)


class ConfirmSchemaRequest(Wire):
    schema_body: Schema
    expected_digest: str
    type_mapping: dict[str, str] = {}
    predicate_mapping: dict[str, str] = {}
    # Explicit human scope for changed semantics; an empty list means all sources.
    affected_sources: list[str] = Field(default_factory=list, max_length=1000)


def invalid(code):
    raise HTTPException(422, code)


def map_schema(schema, types, predicates):
    value = copy.deepcopy(schema)
    value["entity_types"] = [types.get(t, t) for t in value["entity_types"]]
    value["predicates"] = {
        predicates.get(k, k): {
            **v,
            "subject_type": types.get(v["subject_type"], v["subject_type"]),
            "object_type": types.get(v.get("object_type"), v.get("object_type")),
        }
        for k, v in value["predicates"].items()
    }
    for rule in value.get("rules", []):
        rule["predicate"] = predicates.get(rule.get("predicate"), rule.get("predicate"))
    value["evidence_slots"] = [predicates.get(s, s) for s in value.get("evidence_slots", [])]
    return Schema.model_validate(value).model_dump(mode="json")


def schema_semantics(value):
    return {k: v for k, v in value.items() if k not in {"revision", "issue_pack_revision"}}


def check_coverage(rows, sources):
    """All exact source ranges must be accounted for; failed documents remain explicit gaps."""
    by_id = {s["source_id"]: s for s in sources}
    if len(rows) != len(by_id) or {r.get("source_id") for r in rows} != set(by_id):
        invalid("COMPILE_COVERAGE_MISSING")
    gaps = []
    for row in rows:
        source = by_id[row["source_id"]]
        if row.get("status") == "failed":
            gaps.append({"source_id": row["source_id"], "code": "COMPILE_SOURCE_FAILED"})
            continue
        if row.get("status") not in {"complete", "no_facts"}:
            invalid("COMPILE_COVERAGE_INVALID")
        end = 0
        for pair in sorted(row.get("ranges", [])):
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(v) is not int for v in pair)
                or pair[0] != end
                or pair[1] < pair[0]
            ):
                invalid("COMPILE_COVERAGE_INVALID")
            end = pair[1]
        if end != len(source["text"]):
            invalid("COMPILE_COVERAGE_INCOMPLETE")
    return gaps


def candidate_from_drafts(output, sources, manifest, schema, *, types=None, predicates=None):
    types, predicates = types or {}, predicates or {}
    schema = Schema.model_validate(schema)
    by_source = {s["source_id"]: s for s in sources}
    gaps = check_coverage(output["coverage"], sources)
    rejected = {g["source_id"] for g in gaps}
    evidence, evidence_ids = {}, {}
    for raw in output["evidence"]:
        source = by_source.get(raw.get("source_id"))
        if not source or raw["source_id"] in rejected:
            invalid("EVIDENCE_SOURCE_INVALID")
        quote = raw.get("quote", "")
        start, end = raw.get("start"), raw.get("end")
        if not quote:
            invalid("EVIDENCE_INVALID")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or not start < end <= len(source["text"])
            or source["text"][start:end] != quote
        ):
            if source["text"].count(quote) != 1:
                invalid("AMBIGUOUS_EVIDENCE_LOCATION")
            start, end = source["text"].index(quote), source["text"].index(quote) + len(quote)
        item = Evidence(
            evidence_id=digest([source["source_id"], source["revision"], start, end]),
            source_id=source["source_id"],
            revision=source["revision"],
            digest=digest(source),
            start=start,
            end=end,
            quote=quote,
        ).model_dump(mode="json")
        old = evidence_ids.setdefault(raw["evidence_id"], item["evidence_id"])
        if old != item["evidence_id"]:
            invalid("DUPLICATE_EVIDENCE_ID")
        evidence[item["evidence_id"]] = item
    entities, entity_ids = {}, {}
    for raw in output["entities"]:
        item = Entity.model_validate({**raw, "entity_type": types.get(raw["entity_type"], raw["entity_type"])})
        if not item.authority_namespace.strip() or not item.external_id.strip():
            invalid("ENTITY_IDENTITY_REQUIRED")
        if item.entity_type not in schema.entity_types:
            invalid("SCHEMA_INVALID")
        identity = digest([item.authority_namespace, item.entity_type, item.external_id])
        canonical_id = "ent_" + identity[:32]
        if item.entity_id in entity_ids and entity_ids[item.entity_id] != canonical_id:
            invalid("DUPLICATE_ENTITY_ID")
        entity_ids[item.entity_id] = canonical_id
        data = item.model_dump(mode="json")
        data["entity_id"] = canonical_id
        if canonical_id in entities:
            aliases = [*entities[canonical_id]["aliases"], *data["aliases"], data["label"]]
            entities[canonical_id]["aliases"] = list(dict.fromkeys(aliases))[:20]
        else:
            entities[canonical_id] = data
    assertions = {}
    for raw in output["assertions"]:
        data = copy.deepcopy(raw)
        data["predicate"] = predicates.get(data["predicate"], data["predicate"])
        pred = schema.predicates.get(data["predicate"])
        if not pred or data["subject"] not in entity_ids:
            invalid("SCHEMA_INVALID")
        data["subject"] = entity_ids[data["subject"]]
        if entities[data["subject"]]["entity_type"] != pred.subject_type:
            invalid("SCHEMA_INVALID")
        if pred.value_type == "entity":
            if data["value"] not in entity_ids:
                invalid("RELATION_ENDPOINT_INVALID")
            data["value"] = entity_ids[data["value"]]
            if entities[data["value"]]["entity_type"] != pred.object_type:
                invalid("RELATION_ENDPOINT_INVALID")
        expected = {"entity": str, "string": str, "number": (float, int), "boolean": bool}[pred.value_type]
        if not isinstance(data["value"], expected) or pred.value_type == "number" and isinstance(data["value"], bool):
            invalid("VALUE_TYPE_INVALID")
        if not all(q in data.get("qualifiers", {}) for q in pred.required_qualifiers):
            invalid("QUALIFIER_REQUIRED")
        # Model-created system observations and derived proofs are outside this document compiler.
        if data.get("epistemic_kind", "document_fact") not in {"document_fact", "user_claim"} or data.get("premises"):
            invalid("UNTRUSTED_FACT_KIND")
        if not data.get("support_sets") or any(not proof for proof in data["support_sets"]):
            invalid("PROOF_INCOMPLETE")
        try:
            data["support_sets"] = [[evidence_ids[e] for e in proof] for proof in data["support_sets"]]
        except KeyError:
            invalid("PROOF_INCOMPLETE")
        data["assertion_id"] = "ast_" + digest({k: v for k, v in data.items() if k != "assertion_id"})[:32]
        item = Assertion.model_validate(data).model_dump(mode="json")
        assertions[item["assertion_id"]] = item
    if len(assertions) > manifest["max_assertions"]:
        invalid("ASSERTION_BUDGET_EXCEEDED")
    if not assertions:
        invalid("NO_VALID_FACTS")
    return CandidateBundle(
        manifest_digest=digest(manifest),
        entities=list(entities.values()),
        assertions=list(assertions.values()),
        evidence=list(evidence.values()),
        extraction={"mode": "ov_compile", "coverage": output["coverage"], "gaps": gaps},
    ).model_dump(mode="json")


def schema_basis(bundle):
    """Examples from checked facts, never model-written explanations presented as evidence."""
    entities = {e["entity_id"]: e for e in bundle["entities"]}
    evidence = {e["evidence_id"]: e for e in bundle["evidence"]}
    basis = {}
    for assertion in bundle["assertions"]:
        keys = ["type:" + entities[assertion["subject"]]["entity_type"], "predicate:" + assertion["predicate"]]
        for key in keys:
            rows = basis.setdefault(key, [])
            for proof in assertion["support_sets"]:
                for identifier in proof:
                    item = evidence[identifier]
                    if len(rows) < 3 and not any(row["evidence_id"] == identifier for row in rows):
                        rows.append({k: item[k] for k in ("evidence_id", "source_id", "revision", "start", "end")})
    return basis
