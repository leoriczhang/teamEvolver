# Business Prompt and Context Evolution

teamEvolver manages business-Agent Prompt and injection components inside the existing Skill Candidate workflow, validating, publishing and rolling them back together with frozen Skill versions as one execution composition. Candidate generation, True Replay, publication and actual Agent loading are distinct stages. External independent review belongs to the optional enhanced strategy; registration or task completion does not establish business improvement.

## Scope and Prerequisites

| Allowed modules | Current behavior |
| --- | --- |
| `skill` | Existing Skill bundle evolution, including editable resources inside the bundle |
| `prompt` | Changes registered `agent_prompt` and `context_fragment` text; associated Skill bundles and versions stay unchanged |
| `skill`, `prompt` | Joint changes followed by validation of the complete composition; only changed assets receive new versions |

`prompt` means the business Agent's Prompt/injection components. The TE evolution-engine Prompt editor below the scope controls still configures TE's own analysis, proposal generation and judging. The current scope does not automatically modify model weights, arbitrary Agent engine source, tool implementations, permissions or deployment configuration.

Prerequisites are a business execution baseline, verifiable source Evidence and a Replay Adapter supporting target-component replacement and final-request observation. The default reuses Skill static checks, runtime checks and primary True Replay; an independent acceptance Test Dataset is required only by the enhanced strategy. Agents using Skills also need the corresponding team Skills; a pure-Prompt Agent may have zero Skill dependencies. Business Agents must integrate composition pull, rendering, loading and request observation.

## Register an Execution Profile

A tenant administrator can register a baseline in the business Agent execution-profile panel or call `POST /api/skill-evolution/execution-profiles`. Every listed Skill in `affected_skill_ids` must already exist. A pure Prompt Agent sends `[]` without creating a placeholder Skill.

Console registration requires the actual business Prompt; Context fragments depend on it for matching and evolution. The console discovers `{{variables}}`, accepts actual defaults with an explicit empty-value choice, and builds the shared-template input contract. Optional model fields complement the advanced runtime JSON. Preview components, variables, and associated Skills before registration. `POST /api/skill-evolution/execution-profiles/preview` accepts the same body as registration and checks template inputs, Skill dependencies, and ID conflicts without writing versions or creating tasks. Registration rechecks conflicts transactionally; previews do not reserve IDs. Business user prompts and conversation history are per-request inputs, separate from TE engine Prompts.

```json
{
  "profile_id": "business-agent-01",
  "runtime_type": "observed_http",
  "agent_id": "agent-01",
  "user_id": "alice",
  "affected_skill_ids": ["demo"],
  "components": [
    {
      "component_id": "agent-01-main",
      "kind": "agent_prompt",
      "slot": "system",
      "order": 0,
      "content": "Follow the task requirements in the {{domain}} domain."
    },
    {
      "component_id": "agent-01-context",
      "kind": "context_fragment",
      "slot": "context",
      "order": 1,
      "content": "Check the user's explicit constraints before acting."
    }
  ],
  "environment": {
    "request": {"model": "configured-model-id", "temperature": 0, "tools": []},
    "template_variables": {"domain": "document processing"},
    "validation": {"strategy": "standard-v1"}
  }
}
```

The example's `observed_http` is a reference model HTTP transport runtime. It does not establish integration with a real business Agent. `environment.request` supplies the reference adapter's final request fields other than `messages`; model IDs and options must be compatible with its configured endpoint. Keys, passwords and Authorization are excluded from execution profiles; use existing credential management.

Registration limits are identifiers up to 200 characters, 0–50 distinct associated Skills, 1–32 components, nonempty component text up to 256 KiB, and globally unique integer `order` values in 0–1000. Slots are `system`, `developer` and `context`; the reference materializer currently maps `context` to a developer message.

The server computes component hashes, initial revisions and bindings, pins associated Skill versions and tree hashes, and creates a complete release. Re-registering a profile or a component ID already owned in the tenant conflicts; registration is not an overwrite path for published assets. Read profiles with `GET /api/skill-evolution/execution-profiles` and `GET /api/skill-evolution/execution-profiles/{profile_id}`.

Templates support only scalar `{{identifier}}` substitution, with optional surrounding whitespace inside the placeholder. Values come from frozen `environment.template_variables` and may be strings, booleans or numbers. Expressions, conditions and loops are unsupported, and inserted values are not recursively rendered. Missing variables or unsupported syntax block execution. Replay checks template, variable-set and rendered-content hashes against the actual request text.

To share one template across requests with different variables, explicitly set this field in `environment`:

```json
"template_input_contract": {
  "schema_version": "teamevolver.template-input-contract.v1",
  "variable_names": ["domain"]
}
```

Registration requires `template_variables` defaults for exactly these names. The server uses each component ID as its stable `template_id`. Source Trace `input.template_inputs` uses `teamevolver.execution-inputs.v1`: `template_refs` maps component IDs to template ID, revision and content_hash; `template_variables` holds actual request values; `input_messages` contains read-only public user/assistant messages. The host supplies Session/Trace identities, never the generator. A complete model request must match the same source exactly. Offline request reconstruction must be labeled as such rather than presented as an observed historical request.

Case generation retains the existing Checklist and source-anchor mechanism; the host materializes variables and input messages from that anchor. Each Baseline/Candidate pair shares variables, messages, model and tool configuration, changing only candidate component content. Validation freezes and checks per-Case execution-plan and input hashes. Historical Case deduplication also distinguishes variables and input messages. Original answers, scores and subsequent corrections remain evolution Evidence rather than that Case's model input. This adds neither a Judge nor publication requirements and does not change online Agent activation. Profiles without the explicit contract retain fixed-Prompt and Skill behavior.

Shared-template mode selects a bounded window of complete Sessions: all current material remains, followed by history in existing order within the byte budget. Selected and omitted historical sources are frozen in the planning snapshot, which Worker retries reuse. An oversized current batch fails explicitly without consuming material and must be repackaged by the caller. It is never silently truncated or counted as read. Report material coverage separately from final Replay test coverage.

Shared-template Case materialization requires an original target anchor through the existing `recorded_anchor` protocol. Offline independent records retain their isolation checks. A continuous-Session target becomes eligible only after its template references, variables, and actual GENERATION input match the frozen baseline exactly. It remains a recorded Session turn, never a new independent root. The model selects an eligible host-provided anchor; the host materializes the original query, variables, and public inputs. Missing or conflicting evidence fails rather than borrowing another request's values. Checklist, Case counts, and independent cross-Session source thresholds retain existing rules.

## Register Variable Text as Context Assets

Select an existing execution profile in the registration/evolution settings and open the variable-context registration form. Enter a new profile ID, paste a JSON array of variable samples, select managed text variables, preview the derived assets, and register. The source profile and Candidates remain unchanged. Runtime, model settings, Skill dependencies, and other inputs are inherited from the pinned source release. The default edit scope permits only selected Context assets; the combined option also permits the corresponding Prompt templates.

For a source profile that declares `policyText`, both endpoints accept:

```json
{
  "profile_id": "agent-with-managed-policy",
  "source_release_id": "the-selected-current-release",
  "managed_variables": ["policyText"],
  "variable_samples": [{"policyText": "Original policy A"}, {"policyText": "Original policy B"}, {"policyText": ""}],
  "evolution_scope": "context"
}
```

Preview with `POST /api/skill-evolution/execution-profiles/{profile_id}/variable-contexts/preview`; register by removing `/preview`. The server recomputes the result and checks the pinned `source_release_id`, returning 409 if it changed. Samples are bounded to 200 objects and 1 MiB. Selected values must be text variables declared and used by an Agent Prompt. Nonblank values are deduplicated byte-for-byte without whitespace or Unicode normalization; blank values remain ordinary inputs without empty assets. The existing 32-component limit applies.

Alternatively, enter each variable's original text values separately or import a JSON file. Use `variable_values: {"policyText": ["Original policy A", "Original policy B"]}` instead of `variable_samples`, never both. Keys must exactly cover the selected variables, each with 1–200 text values; the combined input remains bounded to 1 MiB. Values register independently without dummy values or a Cartesian product. Preview `input_mode` distinguishes the formats; grouped-mode `sample_count` counts submitted values rather than Sessions or independent Evidence.

Each asset starts at revision 1 with a stable component ID and server-generated `binding.inline_variable`: host Prompt `component_id`, variable `name`, and the original value's UTF-8 SHA256 `source_value_hash`. The frozen original Case value selects the asset, whose literal body replaces the original placeholder without adding another message or recursively interpreting `{{...}}` inside the body. Unregistered values render unchanged. Editing the asset body never changes its source selector.

Registration previews, versions and Candidates group managed values by execution profile and variable name. A variable group is the evolution object; its different values remain members. The Planner diagnoses the group and identifies specific members for the existing exact-edit and Candidate transaction. Four variables with three values each therefore form four groups and twelve value members. Input combinations belong to Cases and do not create eighty-one evolution objects. Members retain their host, original-value selector and individual revision. The group version fingerprint is derived from its frozen member manifest without rewriting old execution plans, component IDs or historical hashes.

`environment.evolution.editable_component_ids` freezes the editable components; omission preserves the previous all-components behavior. Existing Candidate editing, Checklist, True Replay, review, release, and rollback are reused. Registration does not update an Agent, rewrite the source table, or publish a Candidate.

Agents need a compatible assembler. The AgentLab Replay Adapter creates a deterministic transport view for the existing Runtime and separately verifies logical/transport plan hashes and final request bytes. Projections that cannot preserve exact semantics fail before dispatch. This does not enable direct activation of logical inline compositions on an older Runtime; ordinary Agent adoption needs the corresponding assembly integration.

## Connect a Real Business Agent

Expand the real-Agent integration panel for the selected profile and copy its current metadata structure. Read-only `GET /api/skill-evolution/execution-profiles/{profile_id}/integration` returns stable template IDs, revisions, hashes, and managed-variable membership. Its values are registration defaults, not evidence: replace them with actual request variables and public inputs, and record the actual model request in Langfuse GENERATION. Never stamp historical requests with the latest published release.

Place the observation in Trace `metadata.teamevolver_execution`; see the [Agent integration protocol](../agent-integrations/06-protocol-v2.md). TE checks Agent/profile identity, template versions, actual variables, and model-request bytes. Different values of one shared template remain one profile; original-value hashes select managed Context members. Missing or conflicting declarations preserve the source record with a diagnostic rather than inferring templates or fabricating Skills.

The console can check an archived Session via `GET /api/skill-evolution/execution-profiles/{profile_id}/session-match?session_id=...`, showing matching targets and component IDs. This read-only endpoint performs no analysis, consumption, or evolution. A match does not establish unused material, sufficient correction signals, or group thresholds. Select the existing `prompt` module and execution profile to use the same Candidate, Replay, review, and publication workflow.

When a continuous Session uses multiple Prompts, a pure-Prompt profile selects only targets matching its registered version; other Traces remain read-only Context without adding independent source roots. Matching happens before the per-cycle Session cap, so unrelated Sessions neither occupy the cap nor become consumed. Pure-Prompt consumption uses `execution:<profile_id>`: two profiles may independently consume their matching material once, while legacy Skill consumption remains unchanged. Completed legacy pure-Prompt commands identify their historical consumption without rewriting results. Apply append-only migration `023_execution_consumption_scope.sql` before updating API/Workers.

## Central Prompt Version View

Tenant administrators can open **Overview → Prompt versions** to search business execution profiles by profile, Agent or user. **View versions and metrics** opens one dialog for selecting a release and inspecting its immutable Prompt/context text, component revisions and hashes, frozen Skill dependencies, text differences, Replay completion and efficiency metrics, and Agent receipts. The evolution configuration also links directly to the selected profile. Browsing versions only reads records; it does not start Replay or change Candidates, release heads or Agents.

`GET /api/skill-evolution/execution-profiles/{profile_id}/releases/{release_id}` returns the release document and a verifiable summary of its frozen validation, rather than substituting a Candidate's later evaluation. Initial registrations have no A/B metrics. Rollbacks identify their restoration source and do not represent a new Replay. Missing or mismatched historical evidence leaves the text readable while marking metrics unavailable. Selecting another historical text comparison does not change the Baseline used for Replay metrics. Links retain the tenant and may specify `execution_profile` and `execution_release`.

**Current TE release** is the composition offered for pull, not proof that an Agent uses it. Download/activation receipts are `agent_reported` historical events, not final-model-request evidence or a current adoption pointer. Receipts come from the profile's most recent 20 events; absence from this window does not establish that a release was never adopted.

## Select Modules and Trigger

The current-run module selection applies only to that manual execution. The saved-plan selection controls subsequent tasks. The default is `skill`; select a registered business execution profile when enabling `prompt`.

```text
POST /api/skill-evolution/trigger
```

```json
{
  "allowed_modules": ["prompt"],
  "execution_profile_id": "business-agent-01"
}
```

An optional `session_ids` list accepts up to 200 IDs. The endpoint returns `202` and a `job_id`; inspect `GET /api/jobs/{job_id}` for status. Acceptance freezes module selection, the execution baseline and configuration revision. A manual override does not save new defaults. Save a plan using `PUT /api/skill-evolution/settings` with `revision`, `evolve.evolution_allowed_modules` and `evolve.evolution_execution_profile_id`.

A manual trigger can additionally authorize a higher or unlimited candidate-generation Token budget:

```json
{
  "allowed_modules": ["prompt"],
  "execution_profile_id": "business-agent-01",
  "generation_budget": {"max_total_tokens": null, "reason": "Unlimited usage authorized for this experiment; retain actual usage"}
}
```

This authorization requires `prompt` selection and a nonempty reason. A numeric ceiling must exceed the frozen baseline ceiling; `null` removes it. The server derives the previous ceiling and freezes the full authorization into this Worker command without changing the published composition, model parameters or original Evidence. Accounting retains its existing candidate-branch scope and does not claim a total for all services. Without authorization, the original finite budget remains. Generation authorization does not implicitly change the subsequent Replay budget, which uses the existing Candidate validation-budget interface.

`allowed_modules` records permitted changes; `actual_modules` records what changed. A joint selection does not require both kinds of asset to change. Proposal generators and edit endpoints cannot modify an unselected module.

A pure Prompt profile has empty `affected_skill_ids` and release `skill_dependencies`. It uses the same candidate, validation and review endpoints, with `candidate_kind="execution"` and `execution_profile_id`; `skill_name`, `candidate_skill_name`, `current_skill` and `candidate_skill` are `null`. It creates no `SKILL.md` or Skill files. The console displays actual component text differences, component revisions, the baseline release ID, composition hash and zero Skill dependencies; a Skill version is not applicable. Audit `group_key` is `execution:<profile_id>`, rather than a new-Skill discovery group.

Selecting both `skill` and `prompt` for a pure Prompt profile does not invent a Skill. Actual changes record the business components that changed. Existing Skill candidates retain `candidate_kind="skill"`; combined Skill/Prompt evolution continues to validate and pin real Skill versions.

Upgraded environments must apply the appended `022_prompt_only_candidates.sql` schema before running the new API/Worker; application startup does not create tables. The migration preserves existing Skill Candidate types. Pure Prompt Candidates use execution-profile identity without storing a profile name as a Skill name.

## Source Evidence Boundaries

The proposal generator first checks requirements already present in the business baseline. Business conditions, first/subsequent-trigger branches, the meaning of listed messages and their priority are task constraints to preserve; reducing repetition or increasing pass rates does not authorize turning them into optional references. A reconstructed request is not an observed historical request, and adjacent offline records do not establish a preceding conversation turn.

Proposal and Test Dataset synthesis share target-Evidence boundaries and content hashes. External evaluations retain raw text, declared origins and parsed content; conflicting numbers and explanations are not relabeled or resolved using guessed weights. The existing two current Cases, 6–8 Checklist items per Case and historical regression mechanism remain in use. Explicit offline batches additionally follow the recorded-request anchors described in [True Replay](./06-true-replay.md). These constraints do not guarantee better proposals; actual True Replay and review remain necessary.

Sessions with source revisions use stable `provenance.source_session_key` values for independent upstream root Sessions, `target_trace_refs` for current facts, and `context_trace_refs` for explanatory Context. Repeated pulls, growing snapshots and semantic segments do not increase the independent root count. Older Traces remain Context and cannot repeatedly support new Evidence. A valid revision replaces an older Trace fact without creating another independent source.

Unverified source identity, unreliable Trace identity, revision conflicts, Context-only snapshots, and explicitly synthetic, Replay or Candidate-audit sources do not count as valid new Evidence. Legacy Sessions without provenance retain Skill-only compatibility; this does not establish historical business-Prompt provenance. Both evolution thresholds and each synthesized cross-Session Case count independent roots.

Business-Prompt diagnosis requires observed model-request Evidence. Templates, current configuration, `metadata.systemPrompt` and empty system messages do not establish final loading, and today's Prompt cannot fill missing history. A verifiable upstream GENERATION containing a nonempty Prompt that matches the frozen baseline can support diagnosis; it still does not establish a complete replayable environment or actual-loading acceptance.

## Multi-Round Correction During Candidate Generation

Business Prompt and injection components reuse the Skill Plan → Act → Observation → Final loop, exact-anchor editing kernel and configured round/tool-call limits. The Prompt adapter owns component bodies, template variables and source references. It does not impose Skill names, descriptions, bundles or a nonempty `team_skill` classification, and creates no placeholder Skill.

The generator first records a diagnosis or refinement hypothesis with original references, reads the current component, then applies atomic replacements, insertions or deletions. Edit failures, out-of-scope references, changed variables and invalid templates return as Observations for correction within the same generation attempt. A pending candidate supplies the frozen working composition, while the published Baseline remains read-only. Accepted plans retain earlier references and edit support; a later invalid plan does not clear accepted state.

The published Baseline, frozen variables and declared runtime contract establish business facts, roles, permissions, task conditions and exceptions. An unpublished working composition and diagnoses remain hypotheses, not new business authority. Machine assessments, training goals and another participant's wishes do not automatically change the Agent's obligations. Missing history stays unknown, and independent records are not concatenated into facts. Original Evidence may support strategy revisions and consolidation of redundant guidance while preserving business requirements and useful capabilities.

After reading the current body, `replace_component_content` checks `expected_content_hash` and expands a whole-body replacement through the same exact-edit operation, preserving plan authorization, references, variables and atomicity. Final bodies come only from successfully staged tool edits; Final cannot submit bodies directly or bypass writable scope. `inspect_changes` reports raw character counts, UTF-8 byte counts and deltas for the published Baseline, working composition and staged draft. These are neither Token estimates nor length gates.

`validate_changes` performs only deterministic scope, original-target-reference, template-variable, rendering and edit-provenance checks, without another model call. Explicit Final is preferred. On round exhaustion, successfully edited drafts can form a Candidate using the Skill fallback only when the plan, actual changes and every mechanical check are valid. No valid changes or failed checks still produce a skip. This fallback neither publishes nor weakens source or variable constraints. `editor_audit` retains plans, edit counts, Evidence access, edit provenance and composition hashes. Mechanical validity does not establish semantic equivalence or better outcomes; existing True Replay, Candidate Review and Publish still decide business results.

Generation receives complete selected Evidence fields, source boundaries, external assessments and Context-only comparisons. Search is an index; full reads or explicit pages preserve lengths and hashes. Both initial input and accumulated editing history are budget-checked and skip explicitly on overflow, without silent clipping. Context-only content cannot increase source counts or impersonate original corrections.

Subsequent Prompt generation retains measured `interaction_turns`, `tool_call_count` and `total_tokens` for the same Candidate revision. `active_candidate_replay_feedback` adds completed native primary Replay public interactions, actual replies and individual check results for that revision, including normal multi-turn interactions. These are development diagnostics for the next edit, not new business facts, golden answers or independent sources. Locked acceptance, A/A, repeat and single-kind probes are excluded; missing data remains unavailable, without new model-critique calls. Old planning snapshots are never backfilled with later results. Test Dataset synthesis still receives original Evidence and existing basic efficiency feedback, excluding detailed Replay feedback, Candidate text and private acceptance tasks.

Historical records retain necessary original Session bodies and sources for on-demand Prompt expansion; Skill-only input keeps its original summary projection. Old summary-only rows recover original bodies only with matching Session identity and content hash. Unrecoverable records are explicitly unavailable; summaries and current versions cannot fabricate historical content.

After verifying JSON Object support in the gateway, tenant `skills.prompt_editor_json_mode` (default `false`) adds `response_format={"type":"json_object"}` only to Prompt editing requests. `skills.prompt_editor_max_rounds` defaults to `null`, inheriting the Skill budget; explicit values must be integers from 2 to 64. Neither option changes ordinary Skill requests, Replay criteria or mechanical validity requirements.

The Test Dataset is synthesized independently from the same frozen Evidence as the Candidate, retaining two current Cases, 6–8 Checklist items per Case and original source-root requirements. Synthesis makes one generator-model call, followed by deterministic schema, source and task-anchor validation. Ordinary online Prompt synthesis reuses Skill normalization of excess Cases/checks and its deterministic fallback grounded in actual target text; Context, Candidate claims and summaries cannot invent business tasks. Explicit offline anchor records still fail closed instead of using fallback tasks. Checks should assess final replies, public messages, tool events or artifacts and include definitions needed by the Judge; internal drafts and unemitted self-checks are not public completion evidence. There is no separate model review or targeted Case-repair loop.

The Replay Judge receives only the current Case's complete Checklist, public interactions, messages and artifacts, not automatically the source business Prompt, variables or internal observed_requests. Necessary definitions must be self-contained in the fixed query or the complete Checks, without rewriting the query or exposing private Checks to the Agent.

Explicit offline isolated records for business Prompt evolution support `query_mode="recorded_anchor"`. The model selects a valid `anchor_id` and synthesizes Checks without a `query` field. The server verifies frozen targets, Trace/Evidence references, Context boundaries, complete content hashes and byte lengths before materializing the exact original request. Whitespace, line endings and Unicode remain intact. Extra model queries, mismatched anchors or sources fail explicitly, without fuzzy matching, task replacement or summary-based filling. Ordinary Skill and non-offline Prompt synthesis do not enable this protocol. Original wire and materialized-query provenance receipts remain distinct.

Offline synthesis defaults to an output budget of 32768 when no stage-options provider is injected; ordinary Skill and non-offline Prompt synthesis retain 16384. Explicit host options are not overridden. The standalone synthesis entrypoint still does not connect tenant-stored `stage_settings.dataset_synthesis` to that provider, so storage does not establish effective request options. This simplification does not repair that gap. A cap is neither measured usage nor an acceptance gate; truncated or invalid JSON fails without fabricated missing business content.

Replay Judge `satisfied=true` means compliance with the original Check, including prohibitions. Absence of forbidden behavior can support compliance within a complete public observation scope; missing traces cannot establish absence. Conditional Checks retain their conditions without requiring an untriggered branch. This clarification does not flip historical scores or add retries for valid negative judgments. Original Checks, technical-unavailability handling and acceptance thresholds remain separate.

Words such as “especially” and “for example” do not make an exhaustive list by default. The Judge checks the complete original scope and relevant supplied history instead of clearing only examples and declaring compliance with “any/all” requirements. Native `format_diagnostic.request_pin` records hashes of the effective Judge SYSTEM, dynamic JSON Schema, constructed system message and Checklist to distinguish Judge inputs. Its `scope=intended_request` does not prove actual transmission or execution after budget, input or Provider failure. Hashes neither replace business judgment nor store credential plaintext.

When the original Check explicitly prohibits semantic repetition from history, comparison concerns substantive statements or questions. Changes in tone, word order, address, known context or an additional sentence do not exempt a repeated substantive clause. Shared words or topics alone do not establish repetition either. Brief evidence contrasts the relevant historical and current spans without disclosing internal reasoning. The original Check's conditions, exceptions and granularity still control.

## Prepare Locked Acceptance Datasets

This section applies only when `environment.validation.strategy="enhanced-v1"` is explicitly enabled. Default `standard-v1` neither requires locked datasets nor adds configured locked tasks to primary Replay. For the enhanced strategy, create a Test Dataset using the [Replay API](../api/05-replay-branch.md), then reference its ID in `environment.validation.holdout_dataset_ids`. A locked dataset uses the standard format with these source fields:

```json
{
  "schema_version": "team-replay.dataset",
  "dataset_id": "locked-agent-01",
  "name": "Independent acceptance tasks",
  "skills": [{"skill_id": "demo"}],
  "metadata": {"validation_role": "locked_acceptance"},
  "cases": [{
    "case_id": "acceptance-01",
    "query": "Complete an independent document transformation task.",
    "skill_ids": ["demo"],
    "checks": [{"id": "R01", "kind": "output", "text": "Satisfy the supplied task constraints."}],
    "materials": [],
    "provenance": {
      "source_runtime": {"type": "observed_http"},
      "source_session_keys": ["src_actual-independent-source-key"]
    }
  }]
}
```

Use actual source-root IDs in `source_session_keys`; different dataset IDs do not make a shared source independent. Every Case needs a source root. Locked datasets must not share roots with diagnostic/planning Evidence or other selected acceptance datasets. Validation acceptance freezes revisions, hashes, materials and Cases. Unknown or overlapping sources, or a missing locked dataset, block publishing.

The automated proposal generator receives reserved source IDs for exclusion, not locked queries, answers or Checklists. Once a source root has been exposed to one Candidate revision, another Candidate or a new revision cannot reuse it as unexposed acceptance Evidence. Retries of the same revision may reuse it. Renaming or copying a dataset does not erase source-exposure records.

## Replay Adapter and Actual Loading

Adapters supporting execution plans declare these values in `REPLAY_ADAPTER.capabilities`:

```python
{
    "execution_plan": "teamevolver.execution-plan.v1",
    "component_kinds": ["agent_prompt", "context_fragment"],
    "request_observation": "local_dispatch",
    "skill_treatment": True,
}
```

`skill_treatment` declares observed joint Skill/Prompt treatment support. Adapter source still requires the service Root Key and each run pins its revision. Legacy Skill-only adapters remain usable for the original workflow and do not automatically gain Prompt override capability.

`ReplayContext.execution_plan` carries all components, stable slots/order/bindings, target component IDs and the non-target environment. Shared components survive a Case's Skill-subset selection. The Adapter must actually replace the intended content. With `local_dispatch`, it uses the branch's `request_observer` at the final model-transport boundary to dispatch captured immutable request bytes and returns `AgentObservation.execution_receipt`. TE compares local dispatch records and checks full requests, rendered text, order, placement, destination, materials and non-target model/tool configuration. The existing Agent Lab integration uses `remote_attested_dispatch`, verifying a separate control-key signature, nonce and actual final remote requests; see the [Replay integration contract](../api/05-replay-branch.md#pure-prompt-replay-and-the-existing-agent-lab). Self-reported hashes or bodies alone are insufficient.

Missing, ignored, duplicated, overwritten or incorrectly hashed content produces unsupported/inconclusive outcomes. Observation trusts revision-pinned Adapter code; it is not cryptographic attestation of arbitrary uploaded Python or a remote service.

Candidate Replay details expose final-model-request evidence for each branch: `execution_verification`, `execution_plan_hash`, and `observed_requests` with dispatch IDs, request hashes, destinations and complete requests. Without dispatch records, a status string or reported hash does not prove loading. Pure Prompt Candidates still require actual Replay and frozen-strategy publication gates; independent review is required only by enhanced validation. Zero Candidates or baseline registration alone do not establish a complete evolution loop.

`team_replay/adapters/examples/observed_http.py` is a runnable model HTTP transport reference with independent multi-turn histories and final-request capture. It does not execute a business tool loop, Skill scripts or arbitrary Agent orchestration and cannot replace a real business-Agent integration. The current AgentsHub example has no verified independent Prompt/Context override API and explicitly rejects such plans.

The reference adapter calls the Agent model directly over HTTP and accounts for the Agent Replay channel from actual dispatch records and response usage. These requests are excluded from the TE-internal LLM collector, avoiding duplicate accounting for the same request.

## Evaluate and Publish

1. Call `POST /api/skill-candidates/{candidate_id}/evaluate` and wait for completion.
2. Read the complete frozen input from `GET /api/replays/skill/{run_id}/input` and inspect the results. Use the validation response `job_id` as `run_id`; inspect all primary and required regression Cases and A/B requests.
3. Default `standard-v1` aligns with Skill static, runtime and native primary Replay gates, while checking actual Prompt loading, the frozen composition and dependency versions. Call `POST /api/skill-candidates/{candidate_id}/validate` with `{"mode":"auto","candidate_revision":2}`. The Worker publishes from that immutable completed result without another stochastic Replay. A primary `reject` or `inconclusive` is not turned into acceptance, and `force` cannot bypass these necessary Prompt checks.

Only enhanced validation additionally requires locked acceptance, A/A, repeat/single-kind probes and exact-result independent review. After checking with an independent program or domain expert, call `POST /api/skill-candidates/{candidate_id}/independent-validation`. This records external verification; TE does not execute the declared external program:

```json
{
  "candidate_revision": 2,
  "status": "passed",
  "method": "programmatic",
  "evidence_ref": "artifact:independent-review-result",
  "case_ids": ["all-frozen-case-ids-go-here"],
  "notes": "Independent checks and their evidence."
}
```

`status` is `passed`, `failed` or `inconclusive`; `method` is `programmatic` or `domain_review`. `case_ids` must cover every Case frozen by the completed validation. The server derives a v2 receipt from immutable records, binding Candidate revision, the exact `validation_run_id`, validation-command `validation_payload_hash`, complete `validation_result_hash` and input hash. Clients cannot supply these hashes. The receipt is `reviewer_attested`, not automatic model proof. Historical declarations missing these bindings cannot authorize a new publication.

Enhanced validation uses the same `/validate` request after review. The original machine-quality verdict must still be `accept`, and every enhanced gate must pass. A final verdict made inconclusive solely by the missing independent declaration can complete publication once that declaration is attached. Both strategies preserve original validation records; release `validation_ref` points to the actual supporting run. Existing Skill-only Candidates retain their validation/publish path.

Validation acceptance freezes the strategy as `validation_plan.validation_strategy`. New tasks default to `standard-v1`; explicit configuration selects `enhanced-v1`. Previously frozen plans without that marker retain their original enhanced rules, without rewriting or lowering old command gates. Submit a new evaluation to use the new default; historical verdicts do not automatically change.

Acceptance and commit both check Candidate content, budget, applicable review state, configuration revisions, Replay Adapter binding, execution baseline head and Skill dependencies. Any Candidate-state or audit-sequence change after acceptance blocks the old command. Configuration and dependency checks hold database transaction locks; the complete composition and Candidate state commit atomically. A new evaluation removes preceding review authorization; enhanced validation requires review of the new result. Publication creates no additional validation-cost run, while all historical Candidate costs remain cumulative.

Edit business text through `PUT /api/skill-candidates/{candidate_id}/execution-content`, supplying `candidate_revision` and `component_changes`. Each change allows only `component_id`, complete `content`, `reason` and `evidence_refs`. Editing creates a new Candidate revision and clears previous independent review. Changed Cases, Adapter/configuration, dependency versions or execution plans require a fresh evaluation; enhanced validation also requires review of the new frozen result.

## A/A, Repeats and Single-Module Comparisons

Only enhanced validation schedules A/A, repeats and joint-change single-asset-kind comparisons beyond primary Replay; the default does not require these extra runs. A/A uses identical baseline assets; stochastic outputs need not match byte for byte, and unchanged-efficiency inconclusive A/A is not an improvement. Repeats check verdict and completion stability. `only-agent_prompt`, `only-context_fragment` and `only-skill` comparisons observe individual changes without establishing additive improvements or every causal relationship.

| `environment.validation` field | Default | Range / meaning |
| --- | --- | --- |
| `strategy` | `standard-v1` | Default Skill gates; `enhanced-v1` explicitly enables enhanced validation |
| `holdout_dataset_ids` | Empty list | Up to 8 distinct datasets; only enhanced validation requires a valid locked dataset |
| `max_probe_cases` | 4 | 1–8 selected probe Cases, while still covering affected Skills |
| `max_extra_runs` | 5 | 2–8 total A/A, additional repeat and single-kind comparison runs |
| `repetitions` | 2 | 2–3 executions including the primary run |
| `max_total_tokens` | No implicit publication ceiling under standard; 1000000 under enhanced | Explicit 1–10000000 limits remain enforced; Candidate authorization overrides them. The original generation default is unchanged |

`total_model_cost_gate` covers the Candidate's total model cost; its current validation-response field is `cost_gate`. It accumulates generation and Case synthesis, TE judges and feedback, both Agent Replay branches in primary validation and every A/A, repeat and single-kind comparison, and actual provider retries. Earlier validations and generation records for the same Candidate also count against the current frozen budget. Repeated checkpoint reads are deduplicated by TE `attempt_id` and Agent `dispatch_id` rather than counted as new calls.

With a finite budget, missing usage, unknown failed-request cost, incomplete history or unreconciled Worker retry cost keeps the cost gate inconclusive; `known_total_tokens` is only the observed lower bound. Without a cumulative ceiling, `cost_gate.status` can be `passed`, reporting gaps through nonblocking `warnings`. The new default introduces no publication budget gate from an implicit historical default; explicit finite limits and the generation budget remain unchanged.

Budget decisions and accounting completeness are separate: `accounting_status` is `complete` or `incomplete`; `total_tokens` contains the total only when complete, otherwise `null`, while `known_total_tokens` retains the known lower bound. Missing usage is never filled with zero; `te_llm_usage.complete` stays accurate and the console distinguishes unlimited budget success from accounting gaps. Historical results are not rewritten. This ledger does not replace actual Replay completion, metric validity, objective efficiency or observed loading. A/A, repeats and independent review apply only as required by the frozen strategy.

Replay judges and feedback retain their request parameters while using platform dispatch and observed provider usage. Newly executed Cases carry the coverage marker `replay_te_llm_accounting=platform-provider-attempts-v1`. Reusing older checkpoints or validations without that marker produces `replay_te_llm_accounting_unverified`: a blocking reason with a finite budget, or an accounting warning without a ceiling. Rerunning does not recover previously omitted usage, and historical reports are not rewritten as zero-cost or complete-cost records.

The total-cost gate checks cumulative spending when validation results are aggregated. With a finite ceiling, each TE LLM collector stops subsequent model attempts when its own observed usage reaches its budget; probe scheduling stops additional probes according to observed Replay usage. These local checks and concurrent requests already dispatched cannot guarantee that final cumulative spending stays below the fixed budget; earlier generation and validation costs have already been incurred. A total above budget blocks publishing even when the quality checks pass.

## Agent Pull and Atomic Activation

The Agent uses a tenant key and the profile's matching `user_id`:

```text
GET /sync/skills?user_id=alice&execution_profile_id=business-agent-01&execution_capability=teamevolver.execution-plan.v1
Authorization: Bearer <tenant-key>
```

The response adds `execution`, containing `release_id`, `composition_hash`, `profile_id`, `document.execution_plan`, `document.skill_dependencies` and exact-version Skill files in `skills`. Top-level `skills` still lists current team Skills; an execution composition must use its pinned `execution.skills` dependencies rather than arbitrary current heads. ETags include the composition hash, so Prompt-only publishing changes the pull result. Missing capability returns 409; profiles outside the current user or tenant are unavailable.

For a pure Prompt release, `execution.skills=[]` and `execution.document.skill_dependencies=[]` form a valid complete composition. Clients must still validate and load `execution.document.execution_plan`. An empty Skill list must not skip Prompt installation or be replaced with top-level team Skills. Publishing, rollback and task pinning use composition release IDs/hashes without increasing an unrelated Skill version.

`team_skills/execution/client.py:ExecutionReleaseCache` provides a POSIX Agent-side reference cache:

```python
from team_skills.execution.client import ExecutionReleaseCache

cache = ExecutionReleaseCache("/path/to/agent-execution-cache")
installed = cache.install(response_payload["execution"])
task_pin = cache.active()  # Read once at task start, then keep this directory/manifest pinned.
```

The client checks the composition, components and every dependency Skill's version/hash, writes a complete immutable directory and atomically switches the active pointer. A file lock coordinates installers across processes; previous directories remain available to in-flight tasks. This is a rebuildable Agent cache; PostgreSQL remains the server authority. Atomic visibility means readers see a complete composition, not durable recovery from machine/filesystem failure. The business Agent must actually render and load the pinned Prompt/Context and Skills; installing a cache alone does not do that.

Report loading state through `POST /sync/skills/execution-receipt?user_id=alice`:

```json
{
  "profile_id": "business-agent-01",
  "release_id": "released-id",
  "composition_hash": "released-composition-hash",
  "request_id": "activation-event-001",
  "state": "activated"
}
```

`state` is `downloaded` or `activated`. Retry the same `request_id` only with identical content; downloading and activation use separate event IDs. The server marks these receipts `agent_reported`. They describe self-reported exposure/activation and do not establish model consumption or replace Replay's final-dispatch evidence.

## Rollback and Diagnosis

Call `POST /api/skill-evolution/execution-profiles/{profile_id}/rollback` with the target `release_id` and current `expected_release_id`. The Worker restores the composition transactionally, recording a new release and any necessary new asset versions while preserving history. A changed current head conflicts. Agents must pull and activate the restored composition; in-flight tasks keep their existing pins.

If a published Prompt-only Candidate fails later review, first roll its exact publication back to its original baseline. Once that direct rollback command has completed, and no other live Candidate exists for the profile, call `POST /api/skill-candidates/{candidate_id}/resume-after-rollback` with `candidate_revision`, the restored head's `expected_release_id`, corrected `component_changes`, and a `reason`. The server checks the real rollback command, original publication, and complete baseline content. It resumes the same Candidate at a new revision and clears stale validation, review, and adoption state. Immutable releases, Replay results, audit events, and cumulative costs remain. Recovery neither activates an Agent nor publishes the correction. Ordinary published/rejected Candidates remain read-only; this exception only permits eligible Prompt-only recovery.

An administrator calls `POST /api/skill-candidates/{candidate_id}/validation-budget` with the current `candidate_revision`, `max_total_tokens`, and a nonempty `reason` recording authorization. An open execution-aware Candidate can raise its finite ceiling to a larger integer (at most 10 million), or explicitly send JSON `null` to remove its cumulative token ceiling. Omission, strings and large integers do not mean unlimited. Explicit finite budgets and old frozen policies remain unchanged. Revision CAS creates a new revision and audit event, invalidates prior validation/review authorization, and requires fresh evaluation and, for enhanced validation, review. Both execution plans, model parameters and all incurred costs remain unchanged. Once unlimited, this route rejects any further change, including a stale client restoring a finite value; it does not provide a re-limiting operation. Supported amounts are not spending authorization for a particular task.

After explicit cancellation, the next frozen `validation_budget.max_total_tokens`, `validation_plan.probe_policy.max_total_tokens`, acceptance policy, result `cost_gate.budget_tokens` and probe `token_budget` are `null`. Cumulative token-threshold comparisons are disabled; gaps in the cumulative ledger become nonblocking `warnings`, retaining the known lower bound, unknown total and historical deduplication. Timeouts, Checklist, observed execution, current Replay metric validity and efficiency remain unchanged; enhanced probe-count, stability and independent-review requirements are also unaffected by budget cancellation. A passed budget cannot replace those requirements. Both plans retain their original `environment.validation.max_total_tokens`. A null ceiling neither means zero cost nor repairs or retroactively accepts historical results.

| Symptom / reason | Check |
| --- | --- |
| `prompt_provenance_missing` | Verifiable original business-Prompt requests matching the frozen baseline |
| `execution_not_observed`, unsupported | Adapter capabilities, actual replacement, final dispatch and receipt |
| `holdout_source_overlap`, `holdout_already_exposed` | Independent source roots and acceptance-source exposure |
| `locked_acceptance_missing` | Locked metadata, Case sources and `holdout_dataset_ids` |
| `execution_probes_incomplete` | Budget, coverage, observation, A/A and repeat stability |
| `total_model_cost_unverified`, `total_model_cost_unknown` | Check TE/Agent channels and history; without a ceiling, cumulative gaps appear in `cost_gate.warnings`, while real Replay metrics are checked separately |
| `total_model_cost_exceeds_budget`, `worker_retry_cost_unreconciled` | Compare cumulative cost with a finite budget; without a ceiling, retry gaps remain accounting warnings and are never filled with zero |
| `independent_validation_missing`, `independent_validation_inputs_changed` | External review coverage of all current frozen inputs and Cases |
| Version conflict | Execution composition or associated Skills changed during planning, validation or publishing |

## Related Documentation

- [Evolution Loop](./02-evolution-loop.md)
- [True Replay](./06-true-replay.md)
- [Replay and Workbench API](../api/05-replay-branch.md)
- [Skill Pull API](../api/06-skill-sync.md)
- [Team Skill Library](../api/09-skills-admin.md)

Main implementation: `team_skills/execution/`, `team_skills/api/execution_assets.py`, `team_replay/execution_plan.py`, and `team_replay/request_capture.py`.
