# Ontology knowledge operations API

New builds use the [V6 native Compile workflow](../../ontology-integration/45-native-compile-wiki-operations.md): TE recursively freezes directories or individual files, then calls unmodified OV Compile with a versioned Skill. A single scan produces schema proposals and evidence drafts. Human schema confirmation precedes deterministic candidate conversion and publication review.

The [V5 guide](../../ontology-integration/33-v5-contracts-and-operations.md) remains historical; V6 supersedes its embedded extraction path. Publication still uses [trusted + Root Key](../../ontology-integration/38-trusted-root-publication.md), without an independent signing key.

New source-collection endpoints create, list, inspect, retry and cancel durable collections under `/te/enterprise/v1/source-collections`. POST `/jobs` uses `sf.te.ontology.compile.v1` with collection_id. `/jobs/{id}/schema-confirm` confirms a digest-bound proposal; `/jobs/{id}/retry` reconciles uncertain acceptance or explicitly retries failure. Approval requires acknowledge_gaps when coverage is partial.

The native snapshot freeze primitive remains single-file only. Directory builds use source collections, not trailing-slash removal. State and progress survive browser refresh. The model runs in OV Compile; TE model settings do not drive new extraction. Install the bundled ontology-extraction-v1 Skill explicitly. Workspaces require restricted ACLs; ordinary agents must be denied both reads and search.

HTTP deployments use getRandomValues when randomUUID is unavailable. Retried acceptance reuses the persisted request. Model-stub tests, actual model extraction and deployment acceptance are reported separately.

## Build Skill URI

POST `/te/enterprise/v1/jobs` accepts optional `skill_uri`. Missing or blank values use the service default, initially `viking://agent/skills/ontology-extraction-v1`. A Skill directory or its `SKILL.md` is accepted; external URLs, non-Skill namespaces and traversal are rejected. Capabilities returns `compile_skill_uri` for the UI default. The resolved URI is checkpointed as result.skill_uri for retries and recovery. The compiler identity, current ACL and bundled Skill content validation still apply. YAML formatting differences introduced by OV do not count as content changes.


## Temporal gaps and ACL readiness

Assertions come in two kinds. Dated assertions carry `valid_from` as ISO 8601 with a timezone; a timezone-free value is accepted and normalized to Beijing time (UTC+8). Standing assertions — definitions, rules and attributes without inherent time — omit `valid_from` entirely: they are valid from the source document snapshot until `valid_to` or a later revision supersedes them. A non-null `valid_to` must be strictly later than the start. An instantaneous event does not have identical start and end values. TE never fills dates from the current time, document metadata or unrelated facts.

Invalid temporal drafts are quarantined as explicit review gaps while independently valid assertions can form a candidate. Gaps retain subject, predicate, source and evidence identifiers. Schema review exposes `result.assertion_gaps`; candidate conversion adds them to `result.gaps`, so approval requires `acknowledge_gaps=true`. If none remain valid, `COMPILE_ASSERTION_DATES_INVALID` initiates at most two repair compiles. Repair instructions include up to 20 safe field paths/error codes in `repair_issues` and the full `repair_issue_count`, without source text or invalid field values.

OV mkdir asynchronously queues directory context indexing. TE checkpoints `result.acl_wait` and retries only `COMPILE_ACL_TARGET_NOT_READY` with 2–15 second backoff for at most 300 seconds. No source content is written until restricted ACL readback and the ordinary-user denial probe pass. Exhaustion returns `COMPILE_ACL_WAIT_EXHAUSTED`; inspect OV indexing and the target context. Manual retry resumes the same round/workspace with a new bounded wait window, without consuming a model repair round. Permission failures are not treated as readiness delays.

Deploy TE and its bundled ontology-extraction-v1 Skill together. No changes to native OV Compile, mkdir or ACL APIs are required.
