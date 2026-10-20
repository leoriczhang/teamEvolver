# ruff: noqa: F401
from __future__ import annotations

from .compile_strategy_base import (
    _OWNED_COMPILER_POLICIES,
    _OWNED_POLICY_FRAMES,
    _POLICY_BLOCK,
    CASE_BLUEPRINT,
    COMPILE_CONTRACT,
    COMPILE_PLAN,
    DEFAULT_MINING_POLICY,
    EXTRACT_ROUTING_RULES_V2,
    MAX_FILE_MAP_PACKETS,
    MINING_POLICY_SCHEMA,
    MULTI_COMPILE_PLAN,
    MULTI_EVIDENCE_GUIDANCE,
    OUTPUT_LANGUAGE_GUIDANCE,
    STAGE_RULES,
    TE_STAGED_COMPILE,
    _body_sha,
    _efficient_workflow_guidance,
    _minimal_policy_exterior,
    _unique_policy_keys,
    _validate_mining_policy,
    compiler_business_policy,
    compiler_execution_guidance,
    hashlib,
    import_module,
    json,
    mining_policy_block,
    multi_compile_contract,
    multi_compiler_execution_guidance,
    re,
    register_owned_compiler_policy,
    render_case_batch_compiler,
    render_efficient_stage_compiler,
    render_extract_batch_compiler,
    render_merge_batch_compiler,
    render_refinement_plan_compiler,
    render_single_stage_compiler,
    render_stage_batch_compiler,
    stage_compiler_body,
)

DIRECT_WORKFLOW_PROTOCOL = "te-direct-workflow-planning.v1"
DIRECT_WORKFLOW_RULES = (
    "Perform direct workflow planning for packet.stage=plan under te-direct-workflow-planning.v1. "
    "derived_controls.items contains at most 24 assigned original catalog methods, not intermediate "
    "merge proposals or additional original Evidence. Read every assigned item in this one packet. "
    "Use their traceable task, boundary and source lineage to organize complete reusable workflows; "
    "final writers verify business claims against the retained originals. Never invent procedures, "
    "facts, resources, calculations or a resolution for conflicting source statements. "
    "The same purpose and terminal output can have different people, examples, channels, durations, "
    "amounts, optional branches or supporting substeps. Treat those as parameters or source-supported "
    "conditional branches within the complete workflow, not separate Skills by default. Combine an "
    "assigned full workflow with its assigned constituent substeps in one disposition. Do not create "
    "both a complete workflow and a second Skill for its introduction, confirmation, objection "
    "handling or other constituent step just because that step adds detail. An optional extra action "
    "does not by itself change the independent purpose of its parent workflow. Preserve genuinely "
    "different purposes, terminal outputs and normative applicability boundaries; shared names, "
    "source files or individual operations alone never establish equivalence. No fixed corpus-wide "
    "candidate count or arbitrary compression ratio applies. Personal experiences and example "
    "values are supplied inputs, not prerequisites every user possesses or universal business rules. "
    "Do not make canonical boundaries exclude an optional same-workflow branch merely because the "
    "current window does not contain it. Preserve supported conditions and uncertainty accurately. "
    "When a source contains opposing or time-dependent statements, retain their qualifications or "
    "the unresolved conflict; do not select one as an unconditional rule or silently repair it. "
    "Use known_candidates only as exact canonical identity context, never as factual Evidence. An "
    "assigned method belonging to a known complete workflow must reuse that canonical candidate, "
    "including new source-supported examples, parameters, optional branches and substep details "
    "that leave its purpose and boundary stable. Do not mint a new name or duplicate child Skill "
    "to avoid canonical reuse. If canonical business fields truly require correction, use only an "
    "explicitly authorized v3 refinement; neither reuse nor naming authorizes a metadata rewrite. "
    "For a normal result, use derived_controls.selection_schema, either skill-miner.selection.v2 "
    "or skill-miner.selection.v3; when absent use skill-miner.selection.v2. Return exactly "
    "schema_version, input_ids, skills, knowledge, deferred and resolved. input_ids equals the complete "
    "exact derived_controls.input_ids container list, each once, never native OV runtime range IDs. "
    "Only exact assigned items[].item_id values may appear in derived_from. Every assigned method "
    "is disposed of exactly once across all rows, without omission or duplication. related_facts, "
    "provisional_gaps and known_candidates are readonly context, never extra disposition items; "
    "resolved is an empty array. Choose 0-8 complete independent workflows, including reuse and "
    "refinement rows in this per-response maximum. A new skills row has exactly derived_from, name, "
    "task, trigger, inputs, outputs, boundary and rationale. name is a stable lowercase kebab slug "
    "of at most 64 characters. task, trigger, boundary and rationale are nonempty, at most 400 "
    "characters each; inputs and outputs each have 1-16 nonempty strings of at most 240 characters. "
    "A reuse row has exactly derived_from, reuse_candidate_id and reason. Copy an exact assigned "
    "known candidate ID and use a nonempty reason of at most 1000 characters; omit all authoritative "
    "metadata, source_refs and extra fields from that row. Each knowledge or deferred row has "
    "exactly derived_from and reason, consumes one assigned method and has a nonempty reason of "
    "at most 400 characters. Factual lookup can become knowledge; defer only genuinely unsupported "
    "execution, never a missing runtime input value, missing historical metadata or a limitation "
    "of this planning window. Zero skills still requires complete individual method dispositions. "
    "TE derives provenance from the exact item ledger; omit source_refs from normal dispositions. "
    "Do not emit Skill text, case questions, Checklist or file assignments. Keep the entire JSON "
    "within 12000 characters. The schema labels are inline protocol strings, not resources or files. "
    "There is one alternative when this assignment cannot produce a complete bounded normal result: "
    "return exactly schema_version='skill-miner.selection.capacity.v1', input_ids, source_refs and "
    "reason. input_ids equals all exact derived_controls.input_ids, each once. source_refs equals "
    "the complete exact derived_controls.allowed_source_refs, each once, without shortening or "
    "adding references. reason is nonempty, at most 1000 characters, and explains either more than "
    "eight genuinely independent workflows after valid grouping/reuse, or a complete normal "
    "response that cannot fit the 12000-character output limit. TE will split the frozen assignment "
    "and retain every method. Capacity is neither an Evidence gap nor a successful partial "
    "selection: include no skills, dispositions or invented missing-resource explanation in this "
    "alternative. Do not force unrelated workflows together, discard items, truncate a response "
    "or classify executable methods as knowledge merely to avoid capacity. More than eight method "
    "items, personas or source references alone does not demonstrate capacity. Schema mistakes, "
    "unauthorized metadata changes and genuine Evidence insufficiency are not capacity reasons. "
    "For <=8 assigned methods, prefer concise complete dispositions; output-size capacity still "
    "requires an explanation of the irreducible supported content. An irreducible single-method "
    "result must never be concealed by claiming that its source needs supplementation."
)


def render_direct_workflow_compiler(base_body: str, *, refinement: bool = False) -> str:
    """Plan fresh catalog windows directly through the unchanged public file Map/Finalize contract."""
    if type(refinement) is not bool:
        raise ValueError("Direct workflow refinement must be a boolean")
    policy = compiler_business_policy(base_body)
    # Direct planning replaces the intermediate merge stage. Retain its explicit
    # business constraints, while leaving every legacy renderer and frozen body intact.
    keys = ("merge_index", "merge_batch", "direct_workflow")
    if refinement:
        keys += ("selection_refinement",)
    inherited = "".join(
        "Inherited custom policy for " + key + ":\n" + policy["stages"][key] + "\n\n"
        for key in keys if key in policy["stages"]
    )
    refinement_rules = (
        "\nFor explicitly selected skill-miner.selection.v3 only, a refinement skills row has exactly "
        "derived_from, revise_candidate_id, base_metadata_sha256, task, trigger, inputs, outputs, "
        "boundary, rationale and reason. Copy the exact assigned known candidate ID and its "
        "base_metadata_sha256; refinement_allowed must be true and every current derived_from item "
        "must belong to that candidate's exact refinement_item_ids. Replace all six business fields "
        "within the SAME independently executable workflow, preserving supported constraints and "
        "source conflicts. Their normal field limits still apply; reason is nonempty and at most "
        "1000 characters. Explain the current source-backed correction, not merely a preferred "
        "wording. Never mix new, reuse and refinement forms, never include name or case IDs in a "
        "refinement row, and never treat previous fields as factual proof. TE preserves canonical "
        "identity, name and existing case IDs and records the exact base hash and source lineage. "
        "A v2 result cannot contain refinements. Missing authorization cannot be hidden by a "
        "duplicate candidate, fabricated Evidence gap or capacity response."
    ) if refinement else ""
    # The plan renderer supplies its common and plan policy exactly once. This
    # fresh-only child has no old selection or intermediate merge transport rules.
    return render_single_stage_compiler(base_body, "plan", inherited + DIRECT_WORKFLOW_RULES + refinement_rules)


EXTRACT_ROUTING_RULES_V3 = (
    "Return skill-miner.index.v2 with exactly schema_version, methods, facts, deferred, context_refs, "
    "context_uncertain and optional input_ids. The protocol name is an inline JSON string, not a file "
    "or Skill resource. methods rows have exactly task, boundary, source_refs; facts rows have exactly "
    "text, source_refs; deferred rows have exactly reason, source_refs. At most 16 methods, 32 facts "
    "and 16 deferred rows. Each task, boundary, text and reason is nonempty and at most 1000 "
    "characters; the entire canonical JSON is at most 12000 characters. No extra row fields or "
    "aliases are accepted. At least one classification array is nonempty, including facts-only input. "
    "Classify all original coverage without inventing methods, facts or gaps. Every row "
    "source_refs and every context_refs entry uses an exact derived_controls.allowed_source_refs "
    "handle for THIS packet. reference_map binds handles to exact original chunks. Select precise "
    "supporting ranges, not all packet ranges by default. Retain complete assigned coverage across "
    "rows, including all supported method branches. input_ids may be omitted: "
    "TE sets this bookkeeping only after exact assigned source coverage is validated. Never copy "
    "native OV runtime range IDs into content references. "
    "Identify each executable workflow's independent purpose and terminal result in THIS window. "
    "When a complete workflow and its constituent steps are visible together, "
    "emit one method describing that complete workflow, including its supported operations and "
    "boundary. Do not also emit its opening, explanation, collection, confirmation or other included "
    "substeps as separate methods merely because they have their own intermediate outputs or "
    "headings. Different personas, examples, channels, amounts, timing or optional steps "
    "belong to one parameterized method when the independent purpose and terminal result are the "
    "same. Retain ALL assigned branch ranges in method source_refs. Describe the complete supported "
    "procedure and conditions in task/boundary, with example detail in facts. Preserve "
    "different purposes, independently needed terminal outputs and mandatory applicability limits; "
    "shared words, source files, audiences or tools alone do not justify merging. Two complementary "
    "steps at the same level are not equivalent just because both contribute to a broad outcome. "
    "Do not invent a parent workflow or its missing steps across windows. A visible partial method "
    "can retain its supported scope and continuation uncertainty without claiming the whole source "
    "lacks a procedure. Capacity, schema mistakes and a required runtime input without a concrete "
    "value are not Evidence gaps. Do not turn a supported method into deferred or factual lookup "
    "merely to fit the row limit, and do not force different workflows together to fit it. "
    "context_refs is a unique list, possibly empty, of exact handles containing a shared mandatory "
    "condition, exception or overriding rule needed across methods in THIS accepted unit. A rule "
    "must apply to more than one supported method here; proximity or a common heading does not "
    "establish shared applicability. For a facts-only unit, mark actual mandatory or overriding rules "
    "in context_refs even without a method here; TE retains unowned rules for every candidate. "
    "Keep each rule as a fact with exact references. A constraint "
    "needed by only one method "
    "belongs in that method's precise source_refs instead. Ordinary personas, examples, scenario "
    "lists, descriptions, intermediate steps and source headings are not shared constraints. Never "
    "mark every range, the whole document or an example table as shared merely because "
    "they are related. The host routes method-owned context locally; context_refs does not declare "
    "a corpus-wide rule. context_uncertain is a boolean: true only when an actual visible mandatory "
    "constraint has unresolved scope or linkage that cannot safely be localized from this window. "
    "Do not set it true simply because packets are bounded, another document is not copied, a "
    "method continues later or a runtime value is unknown. With no unresolved constraint use false; "
    "with no actual shared constraint use context_refs=[]. Preserve numerical comparisons, units, time qualifications, "
    "exceptions and conflicting statements; never silently choose one interpretation or fix an "
    "example's arithmetic. Personal experience and example values are conditional supplied inputs, "
    "not universal facts or prerequisites. Absence from this window is not "
    "absence from the complete source. A genuine deferred row uses reason, not text; for example "
    "{\"reason\":\"A named attachment is unavailable in the assigned Evidence.\","
    "\"source_refs\":[\"<an exact allowed handle>\"]}. This illustrates shape only; never copy the "
    "placeholder or assume an attachment is missing. If derived_controls.repair_error is present, "
    "correct that exact validation issue without losing supported original coverage. The index "
    "selects source ranges; it never becomes factual Evidence for final business claims."
)


def render_extract_batch_compiler_v3(base_body: str, max_packets: int = MAX_FILE_MAP_PACKETS) -> str:
    """Use local workflow and constraint extraction only for freshly gated source-scope v3 jobs."""
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("An extract batch supports one to eight independent source packets")
    policy = compiler_business_policy(base_body)
    contract = multi_compile_contract()
    contract["synthesize"]["instructions"] = contract["synthesize"]["instructions"].replace(
        "Each Compile reads one TE-owned bounded packet file.",
        f"One Compile reads at most {max_packets} independent TE-owned source packet files. "
        "Each input_unit=file Map assignment reads only its own packet and all its native ranges.",
        1,
    )
    custom = "".join(
        "\n\nCustom policy for " + key + ":\n" + policy["stages"][key]
        for key in ("index", "extract", "extract_batch") if key in policy["stages"]
    )
    contract["synthesize"]["instructions"] += (
        "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"] + custom
        + "\n\nCurrent per-file stage: extract.\n" + EXTRACT_ROUTING_RULES_V3
        + "\n\nEach Map produces one complete index at THAT packet.output_path. FileResponse.files[].inputs "
        "uses supporting native IDs from that same file Map only; content references use that packet's "
        "exact TE reference_map. Never combine sibling packets, source scopes, indexes or output paths. "
        "Finalize collects independent index files. Partial native output is not full batch success: "
        "TE validates and retains each successful index, and repairs only rejected assignments."
    )
    return (
        "---\nname: skill-miner-te-extract-batch-v3\n"
        "description: Produce local workflow indexes with exact source constraint routing.\n---\n\n"
        "# Independent source index assignments\n\n"
        f"Read at most {max_packets} separate bounded packet files. Only packet.evidence is original "
        "Evidence. Each file Map emits only its own packet.output_path. The synthesize instructions "
        "contain complete business policy and the routing index contract. No scripts, auxiliary "
        "resources or whole-collection output planning are required.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


WORKFLOW_IDENTITY_RULES = (
    "Compare only packet.derived_controls.identities under the existing identity grouping contract. "
    "Candidate metadata is planning context, never factual Evidence. Return exactly "
    "{\"schema_version\":\"skill-miner.identity-groups.v2\",\"candidate_ids\":[all assigned IDs],"
    "\"groups\":[{\"candidate_ids\":[known IDs],\"survivor_candidate_id\":\"one grouped known ID\","
    "\"reason\":\"nonempty reason\"}]}. No other top-level or group fields are accepted. candidate_ids "
    "is exactly every assigned required_candidate_ids value once. groups form a complete disjoint "
    "partition, including singletons. Each reason is nonempty and at most 1000 characters. Keep the "
    "entire JSON within 12000 characters. Do not output new IDs, names, fields, facts or operations. "
    "For EVERY assigned identity, compare it against every other assigned identity before forming "
    "groups; do not stop after finding one plausible match or assume separately planned rows are "
    "independent. Check same-journey pairs, subset containment, duplicate terminal results, and a "
    "handoff or closing action already included in another complete phase. A singleton is allowed "
    "only after this overlap audit finds a concrete distinction in invocation trigger, independently "
    "needed terminal result, independently reusable procedure, or mandatory applicability boundary. "
    "Its reason must name the closest overlapping candidate and state that distinction; generic reasons "
    "such as '独立工作流' or 'different workflow' are insufficient. Merge a contained step into the "
    "most complete existing workflow when its canonical fields already cover the step; preserve the "
    "parent as survivor and retain all lineage. Never use a narrow step as survivor of a broader phase. "
    "Compare independent terminal purposes and outputs, rather than the number of listed steps or "
    "literal differences in task/input/output text. An intermediate confirmation, introduction, "
    "collected detail or other nested result is not a different independently needed terminal output "
    "merely because its metadata is shorter than the parent workflow's. Different granularity, "
    "training scenarios, audiences, example values, timing and optional branches alone do not "
    "establish an independent Skill. A complete workflow and its included step can represent one "
    "workflow when the step adds no independent terminal purpose or distinct mandatory boundary. "
    "A genuinely standalone step needs a supported independent invocation and a result needed on "
    "its own, with a materially different purpose or applicability. Preserve those independently "
    "usable workflows. Shared source, topic, tool, audience, generic trust or success language is "
    "insufficient for equivalence. Two complementary same-level steps are not equivalent just "
    "because both contribute to the same higher-level outcome: neither represents the complete "
    "other procedure. Distinct collection, analysis, decision, execution and delivery workflows "
    "remain separate when their terminal purposes and required results truly differ. "
    "survivor_candidate_id must be an exact existing candidate ID inside that group, including a "
    "singleton's own ID. Merging preserves its canonical task, trigger, inputs, outputs and boundary "
    "unchanged. Each identity has readonly authority_priority=[0 for previous authority else 1, "
    "original ordinal]. If a group contains previous authorities, nominate the lexicographically "
    "smallest authority_priority: the host retains that prior authority. Otherwise choose the "
    "existing complete workflow that already covers all members, even when it was created later. "
    "Never select a narrow constituent merely because it appears first. Only merge when the actual "
    "survivor's declared workflow covers all grouped operations, terminal purposes and "
    "mandatory boundaries. Extra source coverage cannot widen a narrow canonical task. Do not "
    "hide a second complete procedure inside the surviving narrow task or assume writers will "
    "silently rewrite its metadata. If containment is clear but a required prior survivor is too "
    "narrow, retain singletons and state that lossless reuse "
    "requires an explicitly authorized canonical correction; do not pretend they have different "
    "independent purposes just because their metadata differs. When genuine equivalence remains "
    "uncertain after the pairwise audit, retain singletons and name the exact missing distinction "
    "evidence in the reason; uncertainty cannot substitute for comparing the pair. The host preserves exact previous "
    "Case IDs, aliases and all original method/source lineage; this comparison cannot authorize a "
    "metadata rewrite or invent missing business Evidence."
)


def render_workflow_identity_compiler(base_body: str) -> str:
    """Review fresh groups.v2 with an explicit existing survivor; old GROUP_RULE stays unchanged."""
    return render_single_stage_compiler(base_body, "identity_group", WORKFLOW_IDENTITY_RULES)


DIRECT_SELECTION_RECOVERY_RULES = (
    "Only plan workflows from THIS assigned packet; do not write Skill text, cases or another stage. "
    "Disposition work is exactly derived_controls.items[].item_id. Before emitting, compare all "
    "assigned item IDs with the combined derived_from lists: each occurs EXACTLY ONCE, with no "
    "omission, duplicate or foreign ID. input_ids is separate container bookkeeping; its completeness "
    "does not prove complete method dispositions. One multi-stage item still has one lossless "
    "disposition, not the same ID in two rows. Never omit methods to repair another row or fit eight "
    "candidates; use the declared capacity alternative when genuinely needed. "
    "A known complete workflow's scenarios, parameters and nested steps use exactly "
    "{derived_from, reuse_candidate_id, reason}, with the exact existing ID and NO copied name, "
    "task, trigger, inputs, outputs, boundary or rationale. A current fragment's subset or clearer "
    "wording does not authorize a narrower same-name task or duplicate child. Keep genuinely "
    "different independent purposes separate. Only explicitly authorized v3 refinement may correct "
    "canonical business fields. "
    "For NEW descriptions, amounts, quantities, ages, durations, periods and allocations are supplied "
    "parameters or explicitly qualified examples, never fixed results for every user. Preserve "
    "product/contract applicability and as-of dates for historical amounts, rankings or status; "
    "a dated snapshot is not an unconditional current fact. Personal experience is optional and "
    "truthful. Unknown runtime facts stay unknown or require input: do not copy another persona's "
    "values, invent attachments, resolve source conflicts or guarantee case-dependent decisions. "
    "Formatting repair retains full assigned coverage and these business boundaries."
)


def render_recovery_workflow_compiler(base_body: str, *, refinement: bool = False) -> str:
    """Add fresh recovery guidance without changing the existing direct renderer or its defaults."""
    rendered = render_direct_workflow_compiler(base_body, refinement=refinement)
    marker = "```json\n"
    start = rendered.rfind(marker) + len(marker)
    end = rendered.index("\n```", start)
    contract = json.loads(rendered[start:end])
    contract["synthesize"]["instructions"] += "\n\n" + DIRECT_SELECTION_RECOVERY_RULES
    return rendered[:start] + json.dumps(contract, ensure_ascii=False, indent=2) + rendered[end:]


LOCAL_SELECTION_CONTRACT_EXAMPLE = json.dumps({
    "schema_version": "skill-miner.selection.v2",
    "input_ids": ["<exact assigned control unit ID>"],
    "skills": [
        {"name": "<new-safe-kebab-name>", "task": "<supported complete task>",
         "trigger": "<supported applicability>", "inputs": ["<required runtime input>"],
         "outputs": ["<terminal result>"], "boundary": "<supported conditions and limits>",
         "rationale": "<source-supported selection reason>", "derived_from": ["<assigned item ID A>"]},
        {"reuse_candidate_id": "<exact assigned known candidate ID>",
         "reason": "<why this method belongs to that same complete workflow>",
         "derived_from": ["<assigned item ID B>"]},
    ],
    "knowledge": [], "deferred": [], "resolved": [],
}, ensure_ascii=False, separators=(",", ":"))

LOCAL_SELECTION_CONTRACT_GUIDANCE = (
    "Normal selection has exactly SIX top-level keys: schema_version, input_ids, skills, knowledge, "
    "deferred, resolved. Reuse is a row INSIDE skills with ONLY reuse_candidate_id, reason, derived_from; "
    "copy an exact ID from derived_controls.known_candidates. There is no top-level reuse array or "
    "kind/name reuse form. A new skills row has the eight fields illustrated below. Shape-only v2 "
    "example; replace all placeholders from assigned controls, never copy its values, invent facts or "
    "fill missing dispositions to imitate it:\n" + LOCAL_SELECTION_CONTRACT_EXAMPLE
    + "\nUse the actual derived_controls.selection_schema; this v2 example does not disable explicitly "
    "authorized v3 refinement or the existing capacity alternative. Every assigned method still has "
    "exactly one disposition; readonly notes and known candidates are not additional assigned items. "
    "All existing field limits, source coverage and canonical authority rules remain in force."
)

LOCAL_SELECTION_CONTRACT_EXAMPLE_V3 = LOCAL_SELECTION_CONTRACT_EXAMPLE.replace(
    '"skill-miner.selection.v2"', '"skill-miner.selection.v3"',
)
LOCAL_SELECTION_CONTRACT_GUIDANCE_V3 = LOCAL_SELECTION_CONTRACT_GUIDANCE.replace(
    LOCAL_SELECTION_CONTRACT_EXAMPLE, LOCAL_SELECTION_CONTRACT_EXAMPLE_V3,
).replace("Shape-only v2 example", "Shape-only v3 example").replace("this v2 example", "this v3 example")


INPUT_CLARITY_INDEX_EXAMPLE = json.dumps({
    "schema_version": "skill-miner.index.v2", "input_ids": [
        "<exact TE packet.input_ids entry A>", "<exact TE packet.input_ids entry B>",
        "<exact TE packet.input_ids entry C>",
    ],
    "methods": [{"task": "<supported method>", "boundary": "<supported applicability and limits>",
                 "source_refs": ["<exact supporting allowed handle A>"]}],
    "facts": [{"text": "<supported fact>", "source_refs": ["<exact supporting allowed handle B>"]}],
    "deferred": [{"reason": "<genuine execution gap in assigned Evidence>",
                  "source_refs": ["<exact supporting allowed handle C>"]}],
    "context_refs": [], "context_uncertain": False,
}, ensure_ascii=False, separators=(",", ":"))

INPUT_CLARITY_EXTRACT_GUIDANCE = (
    "The assigned packet JSON is ALREADY in supplied inputs[].payload.text; runtime line prefixes "
    "are a view, not packet content. Interpret ranges of that same URI in start_char order as one "
    "packet. Read supplied text, not a second file. Never call read_skill_resource for relative packet "
    "or output names, schema/protocol labels, or guessed README/template files; none declares a Skill "
    "attachment. Explicit separate attachments remain governed by existing policy. "
    "Emit ONE FileResponse.files row: path=the actual packet.output_path, inputs=supporting native "
    "IDs from THIS file Map, content=the complete index serialized as a JSON string. Inside content, "
    "source_refs/context_refs use exact derived_controls.allowed_source_refs handles; input_ids, if "
    "included, uses exact TE packet.input_ids, never native IDs. Complete shape-only index example:\n"
    + INPUT_CLARITY_INDEX_EXAMPLE
    + "\nReplace every placeholder. Choose all rows, context_refs and uncertainty from actual Evidence; "
    "empty arrays and false are not default decisions. Do not infer a method, fact or missing material "
    "from this example. A method must be taught by the source through actual actions, ordering or decision "
    "rules. A table of people, products, fields, identifiers or configuration values alone is factual "
    "reference data: do not invent a workflow to create, maintain or validate those records. Preserve "
    "it in facts with its original scope. Configuration or data-management procedures qualify only "
    "when the source actually supplies their steps or decision rules. Keep source contradictions explicit; "
    "do not promote inconsistent example arithmetic into verified rules. Keep all existing exact "
    "coverage, routing, business and 12000-character rules."
)


def _append_fresh_synthesize_guidance(rendered: str, instructions: str) -> str:
    marker = "```json\n"
    start = rendered.rfind(marker) + len(marker)
    end = rendered.index("\n```", start)
    contract = json.loads(rendered[start:end])
    contract["synthesize"]["instructions"] += "\n\n" + instructions
    return rendered[:start] + json.dumps(contract, ensure_ascii=False, indent=2) + rendered[end:]


def render_local_recovery_workflow_compiler(base_body: str, *, refinement: bool = False) -> str:
    """Clarify fresh selection shape while retaining the existing strict planning protocol."""
    return _append_fresh_synthesize_guidance(
        render_recovery_workflow_compiler(base_body, refinement=refinement),
        LOCAL_SELECTION_CONTRACT_GUIDANCE_V3 if refinement else LOCAL_SELECTION_CONTRACT_GUIDANCE,
    )


def render_input_clarity_extract_batch_compiler_v3(base_body: str) -> str:
    """Locate supplied inputs for fresh jobs without rewriting existing extract compiler bodies."""
    return _append_fresh_synthesize_guidance(
        render_extract_batch_compiler_v3(base_body), INPUT_CLARITY_EXTRACT_GUIDANCE,
    )


def render_aliased_workflow_compiler(base_body: str, *, refinement: bool = False) -> str:
    """Use frozen packet-local handles without changing canonical host identities."""
    return _append_fresh_synthesize_guidance(
        render_local_recovery_workflow_compiler(base_body, refinement=refinement),
        "This packet uses short, exact local handles. m001, m002, ... identify assigned method items; "
        "c001, c002, ... identify known candidates; u001, u002, ... identify declared containers/source "
        "references. Copy only handles supplied in THIS packet and respect each field's role. "
        "Never generate, reconstruct or shorten a canonical hash, reuse a handle from an earlier packet, "
        "or use a source handle as a method or candidate ID. Host mappings are immutable and private. "
        "Before emitting, check assigned items in their displayed order: each m handle appears EXACTLY "
        "ONCE across derived_from, and input_ids contains the entire exact declared header. This is "
        "bookkeeping, not authorization to invent a method or defer omitted work. Reuse/revise uses the "
        "exact c handle; authorized refinement still copies its exact base_metadata_sha256. Original "
        "Viking URIs, numerical values and business text are unchanged. Read the packet already supplied "
        "in inputs[].payload.text; it and this compiler have no guessed attachments or same-name resources.",
    )


COMPACT_PLAN_RULES = (
    "Do only this packet's assigned method dispositions, then emit ONE complete FileResponse file at "
    "packet.output_path and finish. Packet text is already in supplied inputs[].payload.text; there are "
    "no guessed packet/schema resources. Inner references use exact packet-local m/c/u handles where "
    "supplied, never native range IDs. FileResponse.inputs uses only the supplied native file-job IDs. "
    "Make one bounded selection; do not enumerate alternative partitions or repeatedly discuss IDs. "
    "Normal JSON uses exactly schema_version, input_ids, skills, knowledge, deferred, resolved. "
    "schema_version copies derived_controls.selection_schema; input_ids copies its exact declared "
    "container list. Every derived_from is a nonempty array of unique exact assigned m alias strings, "
    'e.g. ["m001","m002"], never objects such as [{"item_id":"m001"}] or a single string. '
    "Every items[].item_id occurs EXACTLY ONCE across all derived_from lists. Readonly "
    "related_facts/provisional_gaps/known_candidates are not extra disposition items or factual Evidence; "
    "resolved is []. Preserve all assigned methods and their original source lineage. "
    "Use the smallest complete set of independently reusable workflows. Before destinations, compare "
    "assigned methods for overlap/containment. Scenarios, personas, examples and included steps are "
    "parameters/branches, not destinations. Different purposes, terminal results or mandatory boundaries "
    "stay separate; shared source/topic/tool alone does not prove equivalence. "
    "Every new destination must have (1) its own source-supported invocation trigger, (2) a terminal "
    "result a user needs independently, and (3) a procedure reusable without completing its neighboring "
    "phase. "
    "Extra detail or an intermediate output is not enough. A complete phase "
    "and any assigned method that is only one of its steps must be combined in that phase's single row, "
    "with the constituent methods' source lineage preserved. Do not emit both the whole workflow and "
    "its introduction, collection, confirmation, referral, closing or other included step as another "
    "Candidate. Before finalizing, audit every proposed pair for same-journey overlap, subset containment, "
    "duplicate terminal results and phase actions; merge when a complete survivor covers both without "
    "changing its applicability boundary. Keep separate "
    "only when the triggers, standalone results, reusable procedures or mandatory boundaries materially "
    "differ. A narrower trigger inside a phase and an output filling one of its fields are branches, "
    "unless the source establishes independent use outside that phase and a needed result not already "
    "covered by it. Use no target Candidate count; do not create one Candidate per document, heading or "
    "extraction row. Never force unrelated methods together to meet a quota, invent a parent, "
    "narrow a method to justify a split, "
    "or silently expand its scope. "
    "Known same-workflow destinations use skills rows with ONLY reuse_candidate_id, reason, derived_from. "
    "The ID copies an exact supplied known candidate. Combine this destination's methods into one reuse "
    "row; do not copy or narrow canonical fields. New skills rows have exactly name, task, trigger, inputs, "
    "outputs, boundary, rationale, derived_from. name is a machine identifier: unique lowercase ASCII "
    "kebab slug <=64 chars, e.g. customer-appointment, never a Chinese display title. Follow the output "
    "language policy for human-readable task and descriptions; for zh-CN these and later Skill titles "
    "must remain Chinese. This selection has no title field. "
    "task/trigger/boundary/rationale are nonempty <=400 chars; inputs/outputs each contain 1-16 nonempty "
    "strings <=240 chars. Reuse reasons are nonempty <=1000 chars. knowledge/deferred rows have ONLY "
    "derived_from, reason, consume one assigned method each, reason <=400 chars. Lookup may become "
    "knowledge. Static records or configuration fields without a source-taught procedure also become "
    "knowledge; never invent maintenance, validation or other actions to make them executable. "
    "Defer only essential unsupported business Evidence, never missing runtime inputs or "
    "historical metadata. Do not invent steps, unknown facts, attachments or source-conflict resolutions. "
    "Example amounts/periods/persona values stay supplied parameters or qualified examples; historical "
    "claims retain applicability/as-of dates; personal experience is optional and truthful. "
    "At most EIGHT distinct Candidate destinations can be returned in this response, including known "
    "reuse/refinement. This is a capacity boundary, not an objective to compress every window into eight. "
    "If lossless independent destinations exceed eight, or a complete result cannot fit 12000 chars, "
    "emit the capacity alternative immediately; do not keep searching for forced mergers, omit methods, "
    "return a partial normal result or declare an Evidence gap. More than eight method rows alone is "
    "not capacity. Capacity has ONLY schema_version='skill-miner.selection.capacity.v1', input_ids, "
    "source_refs, reason. Copy the complete exact declared input_ids and allowed_source_refs, each once; "
    "reason is a short nonempty diagnostic <=200 chars, with no workflow-by-workflow discussion. TE "
    "will split the frozen window without losing methods. Normal/Capacity JSON stays <=12000 chars. "
    "Return no prose, Skill body, cases or Checklist. The host keeps all identity, source and later "
    "twelve-Case validation gates; this selection does not publish anything."
)


COMPACT_REFINEMENT_RULES = (
    " For authorized selection.v3 only, refinement rows have exactly revise_candidate_id, "
    "base_metadata_sha256, task, trigger, inputs, outputs, boundary, rationale, reason, derived_from. "
    "Copy the exact known ID/hash, require refinement_allowed=true and all derived_from belong to "
    "refinement_item_ids. All six business fields follow normal limits; reason is nonempty <=1000 chars. "
    "Correct only this same complete workflow from current source-backed proposals. Do not include "
    "name or Case IDs; the host preserves identity/custom Case IDs and audits the exact revision. "
    "No implicit metadata rewrite, guessed authorization, alternate-name bypass or v2 refinement."
)


def render_compact_aliased_workflow_compiler(base_body: str, *, refinement: bool = False) -> str:
    """Future fresh-only helper. Existing renderer implementations/bodies are unchanged."""
    if type(refinement) is not bool:
        raise ValueError("Refinement must be a boolean")
    policy = compiler_business_policy(base_body)
    stages = ("merge_index", "merge_batch", "direct_workflow")
    if refinement:
        stages += ("selection_refinement",)
    inherited = "".join(
        "Inherited custom policy for " + key + ":\n" + policy["stages"][key] + "\n\n"
        for key in stages
        if key in policy["stages"]
    )
    # render_single_stage_compiler appends common + custom plan byte-for-byte exactly once.
    # Only owned default formatting guidance is replaced; no replace() touches user policy.
    return render_single_stage_compiler(
        base_body, "plan", inherited + COMPACT_PLAN_RULES + (COMPACT_REFINEMENT_RULES if refinement else "")
    )
