# Ontology knowledge operations API

New builds use the [V6 native Compile workflow](../../ontology-integration/45-native-compile-wiki-operations.md): TE recursively freezes directories or individual files, then calls unmodified OV Compile with a versioned Skill. A single scan produces schema proposals and evidence drafts. Human schema confirmation precedes deterministic candidate conversion and publication review.

The [V5 guide](../../ontology-integration/33-v5-contracts-and-operations.md) remains historical; V6 supersedes its embedded extraction path. Publication still uses [trusted + Root Key](../../ontology-integration/38-trusted-root-publication.md), without an independent signing key.

New source-collection endpoints create, list, inspect, retry and cancel durable collections under `/te/enterprise/v1/source-collections`. POST `/jobs` uses `sf.te.ontology.compile.v1` with collection_id. `/jobs/{id}/schema-confirm` confirms a digest-bound proposal; `/jobs/{id}/retry` reconciles uncertain acceptance or explicitly retries failure. Approval requires acknowledge_gaps when coverage is partial.

The native snapshot freeze primitive remains single-file only. Directory builds use source collections, not trailing-slash removal. State and progress survive browser refresh. The model runs in OV Compile; TE model settings do not drive new extraction. Install the bundled ontology-extraction-v1 Skill explicitly. Workspaces require restricted ACLs; ordinary agents must be denied both reads and search.

HTTP deployments use getRandomValues when randomUUID is unavailable. Retried acceptance reuses the persisted request. Model-stub tests, actual model extraction and deployment acceptance are reported separately.

## Build Skill URI

POST `/te/enterprise/v1/jobs` accepts optional `skill_uri`. Missing or blank values use the service default, initially `viking://agent/skills/ontology-extraction-v1`. A Skill directory or its `SKILL.md` is accepted; external URLs, non-Skill namespaces and traversal are rejected. Capabilities returns `compile_skill_uri` for the UI default. The resolved URI is checkpointed as result.skill_uri for retries and recovery. The compiler identity, current ACL and bundled Skill content validation still apply. YAML formatting differences introduced by OV do not count as content changes.
