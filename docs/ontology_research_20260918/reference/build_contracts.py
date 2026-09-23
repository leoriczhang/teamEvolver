"""Build draft contracts and synthetic examples. Does not access a network or service."""
from __future__ import annotations
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
DIALECT = "https://json-schema.org/draft/2020-12/schema"

def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}

def arr(item: dict, minimum: int = 0, maximum: int = 10000) -> dict:
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}

text = {"type": "string", "minLength": 1, "maxLength": 2048}
ident = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9_:@./-]+$"}
sha = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
time = {"type": "string", "format": "date-time"}
nullable_time = {"anyOf": [time, {"type": "null"}]}

entity = obj({"entity_id": ident, "entity_type": ident, "aliases": arr(text),
              "external_id": {"type": ["string", "null"]}})
evidence = obj({"evidence_id": ident, "source_id": ident, "source_revision": ident,
                "segment_id": ident, "source_sha256": sha,
                "source_uri": {"type": "string", "pattern": r"^viking://"},
                "source_kind": {"enum": ["document", "system_record", "user_claim", "synthetic"]},
                "status": {"enum": ["active", "revoked"]}})
support = obj({"support_set_id": ident, "evidence_ids": arr(ident, 1, 100),
               "mode": {"const": "all_required"}})
assertion = obj({"assertion_id": ident, "subject": ident, "predicate": ident,
                 "object": ident,
                 "qualifiers": obj({"product": text, "region": text}),
                 "epistemic_kind": {"enum": ["document_fact", "system_fact", "user_claim", "hypothesis"]},
                 "polarity": {"enum": ["positive", "negative"]},
                 "valid_from": time, "valid_to": nullable_time,
                 "support_set_ids": arr(ident, 1, 1)})
# This initial executable subset supports a single conjunctive proof per assertion.
# Production DerivedFact DAGs and alternative proofs are specified in the design, not implemented here.
bundle = obj({"contract": {"const": "sf.ontology.candidate.v1"},
              "tenant": ident, "schema_revision": ident, "base_generation": ident,
              "source_manifest_digest": sha,
              "owner_epoch": {"type": "integer", "minimum": 0},
              "entities": arr(entity, 1), "evidence": arr(evidence, 1),
              "support_sets": arr(support, 1), "assertions": arr(assertion, 1)})
bundle.update({"$schema": DIALECT, "title": "Draft synthetic candidate subset",
               "description": "Contract demonstration only. Does not authenticate source_kind or grant production publication."})

fact_packet = obj({"assertion_id": ident, "statement": text,
                   "qualifiers": obj({"product": text, "region": text}),
                   "evidence_ids": arr(ident, 1),
                   "epistemic_kind": {"enum": ["document_fact", "system_fact", "user_claim", "hypothesis", "derived_fact"]}})
packet = obj({"contract": {"const": "sf.ontology.context.v1"},
              "schema_revision": ident, "semantic_generation": ident,
              "valid_at": time, "known_at": time,
              "authorization_epoch": {"type": "integer", "minimum": 0},
              "renderer_version": ident,
              "facts": arr(fact_packet, 0, 200),
              "missing_evidence": arr(ident), "conflicts": arr(ident),
              "truncated": {"type": "boolean"}, "degraded": {"type": "boolean"},
              "prompt_fragment": {"type": "string", "maxLength": 100000}})
packet.update({"$schema": DIALECT, "title": "Draft ContextPacket transport subset"})

grant = obj({"contract": {"const": "sf.ontology.publication-grant.v1"},
             "tenant": ident, "scope_ref": ident, "prepared_id": ident,
             "candidate_digest": sha, "evidence_manifest_digest": sha,
             "schema_revision": ident, "expected_generation": ident,
             "approval_ref": ident, "nonce": ident, "expires_at": time,
             "signature_ref": ident})
grant.update({"$schema": DIALECT, "title": "Draft publication binding shape",
              "description": "Shape only; no signature verification, authorization or replay protection."})
limits = obj({"max_hops": {"type": "integer", "minimum": 0, "maximum": 2},
              "max_nodes": {"type": "integer", "minimum": 1, "maximum": 100},
              "max_assertions": {"type": "integer", "minimum": 1, "maximum": 200},
              "max_examined_edges": {"type": "integer", "minimum": 1, "maximum": 5000}})
query = obj({"contract": {"const": "sf.ontology.query.v1"}, "scope_ref": ident,
             "schema_revision": ident, "entity_ids": arr(ident, 1, 50),
             "predicates": arr(ident, 1, 20), "valid_at": time, "known_at": time,
             "limits": limits})
query.update({"$schema": DIALECT, "title": "Draft bounded typed query, not SQL/SPARQL"})

example = {
    "contract": "sf.ontology.candidate.v1", "tenant": "demo-tenant",
    "schema_revision": "sf-core@1.0.0", "base_generation": "gen-demo-000",
    "source_manifest_digest": "a" * 64, "owner_epoch": 1,
    "entities": [
        {"entity_id": "demo:issue:receipt-dispute", "entity_type": "IssueDefinition", "aliases": ["合成签收争议"], "external_id": None},
        {"entity_id": "demo:slot:receipt-method", "entity_type": "EvidenceSlot", "aliases": ["合成签收方式证据"], "external_id": None}
    ],
    "evidence": [{"evidence_id": "demo:evidence:1", "source_id": "demo:source:1", "source_revision": "r1",
                  "segment_id": "segment-1", "source_sha256": "b" * 64,
                  "source_uri": "viking://resources/demo/source-r1.md",
                  "source_kind": "synthetic", "status": "active"}],
    "support_sets": [{"support_set_id": "demo:support:1", "evidence_ids": ["demo:evidence:1"], "mode": "all_required"}],
    "assertions": [{"assertion_id": "demo:assertion:1", "subject": "demo:issue:receipt-dispute",
                    "predicate": "requires_evidence", "object": "demo:slot:receipt-method",
                    "qualifiers": {"product": "DEMO_ONLY", "region": "DEMO_ONLY"},
                    "epistemic_kind": "document_fact", "polarity": "positive",
                    "valid_from": "2026-09-18T00:00:00Z", "valid_to": None,
                    "support_set_ids": ["demo:support:1"]}]
}
packet_example = {"contract": "sf.ontology.context.v1", "schema_revision": "sf-core@1.0.0",
                  "semantic_generation": "gen-demo-001", "valid_at": "2026-09-18T00:00:00Z",
                  "known_at": "2026-09-18T00:00:00Z", "authorization_epoch": 1,
                  "renderer_version": "deterministic-demo@1", "facts": [],
                  "missing_evidence": ["receipt_method"], "conflicts": [],
                  "truncated": False, "degraded": False,
                  "prompt_fragment": "缺少必要证据，不能确认本人收件。此例仅演示只读诊断。"}
grant_example = {"contract": "sf.ontology.publication-grant.v1", "tenant": "demo-tenant",
                 "scope_ref": "scope-demo", "prepared_id": "prepared-demo",
                 "candidate_digest": "c" * 64, "evidence_manifest_digest": "a" * 64,
                 "schema_revision": "sf-core@1.0.0", "expected_generation": "gen-demo-000",
                 "approval_ref": "approval-demo", "nonce": "nonce-demo",
                 "expires_at": "2026-09-19T00:00:00Z", "signature_ref": "NOT-A-REAL-SIGNATURE"}
query_example = {"contract": "sf.ontology.query.v1", "scope_ref": "scope-demo",
                 "schema_revision": "sf-core@1.0.0", "entity_ids": ["demo:issue:receipt-dispute"],
                 "predicates": ["requires_evidence"], "valid_at": "2026-09-18T00:00:00Z",
                 "known_at": "2026-09-18T00:00:00Z",
                 "limits": {"max_hops": 2, "max_nodes": 100, "max_assertions": 200, "max_examined_edges": 5000}}

pairs = {
    "candidate_bundle": (bundle, example), "context_packet": (packet, packet_example),
    "publication_grant": (grant, grant_example), "typed_query": (query, query_example)
}
for name, (schema, fixture) in pairs.items():
    (ROOT / "contracts" / f"{name}.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "examples" / f"{name}.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

native = {"from": ["viking://resources/enterprise-private/compile-inputs/job-demo-001"],
          "to": "viking://resources/enterprise-private/compile-staging/job-demo-001",
          "skill": "viking://resources/enterprise-skills/sf-ontology-build-v1/SKILL.md",
          "instruction": "按冻结manifest生成受限候选；不改正式资产；证据不足报告缺口。"}
(ROOT / "examples" / "native_compile_request.json").write_text(json.dumps(native, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# This intentionally describes a narrow draft interface, not existing endpoints.
request = obj({"contract": {"const": "sf.ontology.context-request.v1"}, "scope_ref": ident,
               "issue_pack_revision": ident, "entity_refs": arr(ident, 1, 50),
               "claim": {"type": "string", "maxLength": 16000},
               "observation_handles": arr(ident, 0, 100), "purpose": {"const": "read_only_diagnosis"},
               "token_budget": {"type": "integer", "minimum": 1, "maximum": 16000}})
api = {"openapi": "3.1.0", "info": {"title": "SF OV Enterprise Ontology draft", "version": "0.1.0",
       "description": "Proposed endpoints. No server is implemented in this package. IAM and authority checks are mandatory server-side."},
       "servers": [{"url": "/"}], "security": [{"BearerAuth": []}],
       "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
                      "schemas": {"TypedQuery": query, "ContextRequest": request, "ContextPacket": packet}}, "paths": {}}
for path, operation, req_ref, result_ref in [
    ("/api/v1/enterprise/ontology/query", "ontologyQuery", "TypedQuery", None),
    ("/api/v1/enterprise/context/compose", "ontologyCompose", "ContextRequest", "ContextPacket")
]:
    result_schema = {"$ref": f"#/components/schemas/{result_ref}"} if result_ref else {"type": "object", "description": "Authorized facts, proofs, generation and cursor; full response contract pending integration."}
    api["paths"][path] = {"post": {"operationId": operation,
        "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{req_ref}"}}}},
        "responses": {"200": {"description": "Authorized result; unknown or truncated may be valid", "content": {"application/json": {"schema": result_schema}}},
                      "401": {"description": "Authentication required"}, "403": {"description": "Not permitted"},
                      "412": {"description": "Current evidence or policy invalid"}, "422": {"description": "Invalid contract"},
                      "429": {"description": "Server-enforced budget exceeded"}, "503": {"description": "Authority unavailable; fail closed"}}}}
(ROOT / "contracts" / "openapi-proposed.yaml").write_text(yaml.safe_dump(api, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("Built 4 draft JSON Schemas and fixtures, a native Compile example and a 2-endpoint OpenAPI draft.")
