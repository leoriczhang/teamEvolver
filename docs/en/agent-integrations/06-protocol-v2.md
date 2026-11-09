# Agent Protocol v2

The Agent data plane uses `Authorization: Bearer tevt_...` and a `user_id` declared in the request. The server determines the tenant from the tenant key and the OpenViking Account from the tenant's effective configuration. A caller holding the tenant key can act for any user in that tenant, with no Agent registration or user mapping. Console accounts and Agent integration are independent.

## Identity

The only principal is `(tenant_id, user_id)`, resolved by `teamEvolver/security/agent_principal.py:resolve_agent_principal`. `user_id` rules (`normalize_user_id`):

- A non-empty string of 1–160 characters after NFKC normalization and trimming.
- Only letters, digits and `_`, `-`, `.`, `@`.
- Not `.` or `..`, and must not contain `..`.

Violations return `400 USER_ID_INVALID`; a missing value returns `400 USER_ID_REQUIRED`. Same-named users in different tenants are isolated. Clients cannot override the tenant or Account; an `X-Tenant-Id` different from the key's tenant returns 403.

## Data Plane

| Endpoint | Identity field | Purpose |
| --- | --- | --- |
| `POST /ingest_session` | Body `runtime_context.user_id` | Report Sessions |
| `GET /internal/agents/context/describe` | Query `user_id` | Context capabilities and scopes |
| `GET /internal/agents/context/skills` | Query `user_id` | Skill list and references |
| `POST /internal/agents/context/resolve`, `read`, `remember`, `forget` | Body `user_id` | Retrieval, reads, personal Memory writes and deletes |
| `POST /internal/agents/context/sessions/start`, `append`, `commit` | Body `user_id` | Context Session |
| `GET /sync/skills` | Query `user_id` | Team Skill bundles |

`runtime.type` is used only for analysis, display and Replay selection, never for authentication. Tenant keys can also access `/status`, `/history` and `/api/datasource/pull`; other console endpoints return 403.

## Minimal Session

```json
{
  "schema_version": "teamevolver.agent-session.v2",
  "protocol_version": "2.0",
  "session_id": "task-001",
  "runtime": {"type": "my-agent"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{"turn_num": 1, "prompt_text": "Analyze cost", "response_text": "Cost report done."}]
}
```

## Schemas

- [Session v2](../../schemas/agent-session-v2.schema.json)
- [Context request v2](../../schemas/agent-context-request-v2.schema.json)
- [Context result v2](../../schemas/agent-context-result-v2.schema.json)
- [Context snapshot v2](../../schemas/agent-context-snapshot-v2.schema.json)

## Original Requests and Source Declarations

A turn may report `request_context`, but the server retains it under `reported_request_context`, with an effective level of `agent_reported` and completeness `unknown`. A reported `final_request` / `complete` does not prove that the server observed the final model request. Empty system content is not replaced with invented text.

Client-supplied `provenance`, `source_identity`, `analysis_context`, and reserved source fields in `meta` are retained only under `reported_provenance`. They cannot set server `identity_verified`, select internal snapshot references for analysis, or turn one report into independent sources. Data source Pull derives source roots, factual revisions, target Trace references and context Trace references from the bound Adapter and upstream project identity; see [Session Source Revisions](../concepts/05-sessions.md#continuous-sessions-and-source-revisions).

Required v2 fields and Push idempotency remain unchanged: the same `session_id` in a tenant reuses the first acceptance. Incremental intake of a continuous upstream Session uses data source Pull. The protocol introduces no explicit reanalysis endpoint.

## Business Prompt and Context Attribution for Real Agents

A real business Agent can send actual execution records to the configured Langfuse deployment without an Excel conversion or fabricated offline Session. An administrator first registers its business Prompt template and evolvable Context variables, then copies integration information from the execution profile (`GET /api/skill-evolution/execution-profiles/{profile_id}/integration`, console authorization). Agent credentials, runtime identity and business-asset registration remain separate; this endpoint does not create a new authentication principal.

Each actual interaction Trace uses this structure in `metadata.teamevolver_execution`. Read template identities, revisions and hashes from the combination actually loaded; record variables and public history from this specific request rather than repeatedly sending registration examples.

```json
{
  "schema_version": "teamevolver.execution-observation.v1",
  "execution_profile_id": "business-agent-01",
  "agent_id": "agent-01",
  "runtime_type": "my-agent",
  "release_id": "actually-loaded-release-id",
  "composition_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "template_inputs": {
    "schema_version": "teamevolver.execution-inputs.v1",
    "template_refs": {
      "agent-01-main": {
        "template_id": "agent-01-main",
        "revision": "1",
        "content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    },
    "template_variables": {"domain": "actual domain content for this request"},
    "input_messages": [{"role": "user", "content": "actual public preceding input"}]
  }
}
```

The hashes above illustrate field format and must not be used as real values. A fixed Prompt without template variables may omit `template_inputs`. `release_id` and `composition_hash` remain upstream declarations. Attribution verifies registered template identity, revision, content hash and the actual model input; these declarations do not establish that the whole released combination was correctly activated.

Trace `input` records the current user input (for example `{"messages":[{"role":"user","content":"Current question"}]}`); `output` records the actual reply. The corresponding GENERATION observation retains the complete ordered model request in `input.messages`, including rendered business Prompts, injected content, public preceding messages and the current input. A Prompt name in metadata is insufficient. Leading and trailing whitespace in the current input remains intact. Preserve actual user corrections, feedback and replies as original interactions; do not fabricate grades or recycle Replay output as new production Evidence.

After data source Pull, these Sessions use the existing mandatory analysis and Candidate pipeline. Once the template and request agree, different variable values still belong to one shared template. A registered Context value selects its component by the original value of that variable; variable combinations do not become new objects. Unknown values do not automatically create evolvable components. A project conflict, mismatched template version, variables inconsistent with the model request or conflicting metadata keeps the original Session available but excludes the affected Trace from this profile's evolution Evidence; `request_context.execution_observation_errors` retains metadata parsing diagnostics.

Existing flat `metadata.execution_profile_id` / `agent_id` and offline `trace.input.template_inputs` remain supported; simultaneous declarations must agree. A continuous conversation keeps a stable `sessionId` and a unique Trace ID per actual interaction. Repeated pulls retain one source root, new Traces become targets and earlier conversation remains Context. Requests reported only through `POST /ingest_session` retain the `agent_reported` boundary above; this metadata cannot upgrade them into server-observed proof.

## Boundaries and Constraints

- The Session envelope accepts only `schema_version=teamevolver.agent-session.v2` and `protocol_version=2.0`; `runtime.integration_id` and `runtime_context.external_subject` belong to the retired Agent registration protocol and return 400 when present.
- Team Skills are delivered only by pull through `GET /sync/skills`.
- Only personal Memory can be written; team Memory, team Skills and team Resources are read-only for Agents.
- Context references expire after 15 minutes and can only be used by the `(tenant_id, user_id)` they were issued to.

See [Session Ingestion](../api/03-session-ingest.md), [Agent Context](../api/04-context-workspace.md), [Skill Pull](../api/06-skill-sync.md) and [Replay Adapter](../api/05-replay-branch.md).
