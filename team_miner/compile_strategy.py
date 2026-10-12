"""Finite, provenance-preserving default plan for the OV Skill Candidate compiler."""

import json

from .output_language import OUTPUT_LANGUAGE_GUIDANCE

COMPILE_PLAN = (
    "records = p.map(sources, task=contract.extract)\n"
    "groups = p.shuffle(records, by=contract.routing)\n"
    "work = p.reduce(groups, task=contract.reduce)\n"
    "files = p.map(work, task=contract.synthesize)\n"
    "p.finalize(files, into=target)"
)

COMPILE_CONTRACT = {
    "extract": {
        "instructions": (
            "Read the complete assigned file, preserving coverage of every supplied range. "
            "Return one concise source-grounded fact record per assigned inputs[].id, with only "
            "that exact ID in top-level inputs. Keep boundary context and cross-range conditions "
            "explicit; when a fact genuinely depends on neighbouring ranges, include those IDs too. "
            "Do not bind every fact record to every range of the file. Preserve actionable rules, numbers, "
            "units, conditions, exceptions and exact source URIs in payload.facts. "
            "Omit optional evidence_spans entirely; the runtime retains the full original evidence. "
            "Never emit a standalone evidence_spans object. Each record is one complete JSON object "
            "with top-level inputs, routing_text, scope and payload.fields.facts. For example, with "
            "an actual assigned ID substituted for source-id: "
            '{"inputs":["source-id"],"routing_text":"source facts","scope":{},'
            '"payload":{"fields":{"facts":"Concise facts and exact source URI."}}}. '
            "Do not close the record before payload ends; verify the entire records array is valid JSON. "
            "Human supplements preserve their author, timestamp and references; label unverified assertions "
            "and conflicts explicitly. Prior round Skill Candidates and review comments are revision "
            "context, never trusted Evidence or business facts. Do not create ready drafts or final files "
            "in this step. "
            + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "records",
        "execution": "direct",
        "input_unit": "file",
        "fields": {"facts": "Source-grounded rules with applicability, exceptions and exact source URIs."},
    },
    "routing": {"mode": "all"},
    "reduce": {
        "instructions": (
            "Return exactly seven compact work records, one per concrete required_path. "
            "Plan only file assignments and brief fact digests, never write file content, full case questions, "
            "rubrics or requirements here. Each payload contains only file_path, file_role, "
            "skill_name, case_set='source-grounded-12-v1' and a brief trusted fact_digest. "
            "Use the same chosen skill_name as the required_paths. "
            "Each record.inputs must name all and only supplied extraction records needed by that file. "
            "Select by the actual facts required, never by arbitrary input count. Comprehensive evidence "
            "and Skill files may need every range; evaluation should cite only facts its checks need. "
            "Case files must retain every range supporting their chosen scenarios, conditions and exceptions. "
            "Do not copy the complete input list into every work record by default, and never omit "
            "a needed source merely to reduce prompt size. "
            "The runtime propagates their original source ranges, so facts remain in shared "
            "source storage and writers can read assigned original evidence. "
            "Omit optional evidence_spans; never fabricate range IDs or lines. "
            "fact_digest carries only concise facts already supplied by selected extraction records, "
            "including exact conditions and source range IDs; do not create full questions or rubrics. "
            "Do not copy full source text, source URI lists, package outlines or case blueprints "
            "into payload: writers receive those through the runtime and compiler instructions. "
            "Target under 1800 output tokens for all seven records; this is a planning target, "
            "not permission to omit any file or supporting input. "
            + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "records",
        "execution": "direct",
        "fields": {
            "file_path": "One concrete path from the seven required_paths.",
            "file_role": "One of evidence, gaps, review, skill, evaluation, cases_json, cases_markdown.",
            "skill_name": "The same selected package name in every work record.",
            "case_set": "The literal source-grounded-12-v1, defined in compiler instructions.",
            "fact_digest": (
                "Brief trusted selected facts with conditions and assigned source IDs; no raw text or full rubric."
            ),
        },
    },
    "synthesize": {
        "instructions": (
            "Write exactly the one complete file assigned by inputs[].payload.fields.file_path. "
            "Use the source-grounded-12-v1 case blueprint in the compiler instructions. "
            "Start from the trusted fact_digest and supplied original_evidence. Read its runtime-provided "
            "source_ranges and exact original evidence when necessary, preserving source conditions. "
            "Do not try to read sibling work records or resolve case_set as a file or tool resource. "
            "Read only the assigned source ranges needed to verify this file. "
            "Never reread a range already supplied complete in original_evidence or earlier tool results. "
            "Evaluation describes the evaluation method; do not expand it into a second full source digest. "
            "For analysis/review.json, emit the skill-miner.review.v1 contract with concrete questions "
            "and exact source_refs; an empty questions array is allowed only when no unresolved "
            "Evidence gaps remain. "
            "Other required_paths belong to other assignments. "
            "Return files with exactly one entry at that assigned path, not a partial page. "
            "A successful emit ends this assignment immediately: it does not append files or "
            "promise further work. Put complete content and supporting supplied record IDs in "
            "that single emit; every file.inputs must reference exact assigned input IDs. "
            "Keep output concise enough for the configured output limit. Cite exact original "
            "source URIs; do not replace them with shortened filenames. "
            "For benchmark.json, use the shared team-replay.dataset document contract. Each case MUST "
            "have checks with distinct nonempty text strings and output or trajectory kinds. "
            "Aim for 14-16 independent source-grounded requirements per case, leaving a margin "
            "above the mandatory minimum of 12. Before emit, count each case separately after "
            "deduplicating its output check texts; a single case with 11 checks fails the entire bundle. "
            "Use a richer source-supported scenario when needed to cover enough real checks. "
            "Never pad with generic checks or invent facts to meet the count. "
            "BENCHMARK.md must use the same case IDs and source-grounded checks. "
            + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "files",
        "execution": "direct",
    },
    "output_format": "files",
}


CASE_BLUEPRINT = (
    "source-grounded-12-v1 is shared compiler guidance, not a stored file or evidence ID. "
    "Use the same twelve ordered case IDs case-01..case-12 across the JSON, Markdown and evaluation. "
    "Theme order: product identity/version; intended use and product boundaries; service regions/routes; "
    "delivery timing; pricing; billable weight/rounding; volumetric rules; weight/size/acceptance limits; "
    "surcharges or documented alternatives; pickup workflow; transfer/security/exception handling; "
    "delivery/signature/aftercare. Spread scenarios across the supplied products. "
    "Themes are question topics, not business facts; if a topic is absent, use an explicitly "
    "source-supported boundary comparison instead of inventing rules. "
    "Keep the package structure fixed: SKILL.md gives source-grounded operating guidance; "
    "EVALUATION.md explains how to evaluate these cases; benchmark.json and BENCHMARK.md "
    "cover the same ordered cases. Each final case still needs at least twelve concrete, distinct "
    "source-supported output checks and exact provenance.session_ids; generate full questions and "
    "requirements only in final file writing. Use exact numeric conditions from original evidence, "
    "not generic repeated criteria. The shared themes do not replace semantic consistency review."
)


def compiler_execution_guidance() -> str:
    """Describe existing OV operators; do not override runtime validation or execution."""
    return (
        "\n## Evidence and tool contract\n\n"
        "The complete selected SKILL.md is already supplied. This compiler has no attachments or scripts. "
        "Do not call read_skill_resource or run_skill_script. Do not read the Skill name, README.md, "
        "source paths, templates or guessed files as Skill attachments.\n"
        "Read supplied inputs and original_evidence first. Only when necessary evidence is absent, "
        "call read_evidence with an assigned source_range ID from inputs[].id or "
        "inputs[].payload.source_ranges; omit line bounds to read the complete shard. "
        "A Viking URI is a citation, not an evidence ID or a Skill-relative path.\n"
        "Each intermediate record MUST contain top-level inputs: a nonempty array of exact supplied "
        "inputs[].id values. Do not put inputs inside payload. Omit optional evidence_spans entirely; "
        "never guess line numbers. Each final file also needs supporting input IDs in file.inputs. "
        "Use the runtime emit schema; do not wrap records/files in an invented schema.\n"
        "\n## Shared final-file case blueprint\n\n" + CASE_BLUEPRINT + "\n\n## Finite execution plan\n\n"
        "This task creates one candidate from one frozen snapshot, not many separately routed assets. "
        "Use exactly the five-step plan below. Use routing.mode=all for shared evidence planning; "
        "then Map the seven file work records independently. "
        "do not use semantic routing, related IDs, against=target, extra synthesis rounds or ready drafts. "
        "Choose one skill name at planning time and list all seven concrete required_paths from the output "
        "contract under that directory. Replace <skill-name>; do not emit placeholder paths. "
        "Preserve the full original output contract and all source-grounding requirements.\n\n"
        "```python\n" + COMPILE_PLAN + "\n```\n\n"
        "Use these transform settings in the planner's contract (add the seven required_paths):\n\n"
        "```json\n" + json.dumps(COMPILE_CONTRACT, ensure_ascii=False, indent=2) + "\n```\n"
    )


def multi_compiler_execution_guidance() -> str:
    """One bounded capability planner followed by independent file writers."""
    import copy

    contract = copy.deepcopy(COMPILE_CONTRACT)
    contract["reduce"]["instructions"] = (
        "Reduce is a capability planner and file dispatcher, NEVER a file writer. "
        "Return records only. Omit ALL ready_* fields, including top-level ready_content, "
        "ready_content_ref and ready_path; these runtime shortcuts are forbidden in this stage. "
        "Do not put Markdown bodies, finished JSON documents, full case questions, checks, "
        "requirements, rubrics or other final content anywhere in a record, including fact_digest. "
        "First identify independently executable task methods across ALL extraction records. "
        "Choose 0-8 Skill Candidates based on trigger, inputs, outputs, independent execution and "
        "independent True Replay validation, never one per document or topic. Merge overlapping methods; "
        "keep steps of a single workflow together. Pure facts without an executable method are deferred. "
        "Choose stable lowercase kebab-case names. Preserve prior names where scope remains stable and "
        "explain splits, merges or removals. Do not invent methods merely to meet a count. "
        "Then return exactly four shared analysis work records (evidence-map.md, gaps.md, review.json, "
        "capability-plan.json) plus four work records per selected Skill "
        "(SKILL.md, EVALUATION.md, benchmark.json, BENCHMARK.md). Paths start with analysis/ or "
        "compiled_skill/<chosen-name>/. No other paths. Every record has payload.fields.file_path, "
        "file_role, skill_name (empty for shared analysis) and fact_digest. "
        "The other fields are role-specific and MUST be omitted when not applicable, not emitted empty. "
        "Only the plan assignment has capability_plan: the complete plan object, exactly once. "
        "Each candidate's four assignments carries only that candidate's short capability object "
        "with name, task and boundary, at most 240 characters of descriptive text in total. "
        "Do not repeat its full plan entry or any sibling's capability. Shared analysis has no capability. "
        "Only cases_json and cases_markdown have case_set='task-specific-12-v1' and case_blueprint. "
        "All other assignments MUST omit case_set and case_blueprint. "
        "For shared analysis use only a concise candidate-name index in fact_digest. "
        "Do not duplicate the whole plan across assignments. Fix all names, boundaries and source_refs "
        "before any writer runs. Plan schema is skill-miner.plan.v1; "
        "skills entries contain name, task, trigger, inputs (array of strings), outputs (array of strings), "
        "boundary, rationale and source_refs (nonempty exact original Viking URIs); deferred entries "
        "contain reason and source_refs. Empty skills requires a nonempty deferred list. "
        "Use original source citations, never intermediate IDs as source_refs. "
        "Assign every work record all and only extraction IDs supporting its facts in top-level inputs. "
        "Shared analysis records must cover the entire corpus. The runtime propagates original source "
        "ranges from these IDs to each writer. Never omit supporting inputs to shorten planning output; "
        "writers must retain conditions and exceptions from that original Evidence. "
        "fact_digest is a brief source locator and task-specific fact summary, "
        "at most 160 characters, not copied source text, URI lists, outlines or final content. "
        "task-specific-12-v1 means twelve ordered case IDs case-01..case-12 chosen for this candidate's "
        "own task, not logistics product topics. Each case_blueprint item contains ONLY case_id and "
        "a short scenario of at most 48 characters. Repeat this same twelve-item outline ONLY on "
        "the candidate's cases_json and cases_markdown assignments, never on SKILL or EVALUATION. "
        "Do not include conditions, source_refs, query, checks or requirements in case_blueprint. "
        "Writers recover exact conditions, exceptions and source URIs from the trusted fact_digest, "
        "assigned record.inputs and original Evidence; full questions and checks are written only there. "
        "Keep candidate scopes disjoint except explicitly explained shared Evidence. "
        "Target at most 8000 output tokens for the ENTIRE Reduce emit, including every record, "
        "not 8000 per candidate. Field limits are maxima, not target lengths. Compress descriptive "
        "text and avoid repetition before emit; retain all chosen candidates, required assignments, "
        "source_refs and supporting input IDs. Never reduce coverage or invent facts to fit this budget. "
        "Before emit verify: 4+4N records, one complete plan, blueprints only in the two case assignments "
        "per candidate, no ready_* fields and no final file content. "
        + OUTPUT_LANGUAGE_GUIDANCE
    )
    contract["reduce"]["fields"].update({
        "file_path": "One shared analysis path or one required path within a planned candidate.",
        "skill_name": "Chosen candidate name, or empty for shared analysis.",
        "fact_digest": "At most 160 characters of trusted fact summary/locator; no final content or URI lists.",
        "capability_plan": "Complete skill-miner.plan.v1 object ONLY on the plan writer; omit everywhere else.",
        "capability": (
            "Only name, task and boundary for this candidate, text <=240 characters; omit for shared analysis."
        ),
        "case_blueprint": (
            "Only cases_json/cases_markdown: same 12 items containing only case_id and scenario <=48 characters; "
            "omit everywhere else. No conditions, source_refs, query or checks."
        ),
        "case_set": "Only cases_json/cases_markdown: task-specific-12-v1; omit everywhere else.",
        "file_role": "evidence, gaps, review, plan, skill, evaluation, cases_json or cases_markdown.",
    })
    contract["synthesize"]["instructions"] = contract["synthesize"]["instructions"].replace(
        "Use the source-grounded-12-v1 case blueprint in the compiler instructions.",
        "Use the assigned short capability and trusted fact_digest. "
        "Only case-file writers receive case_blueprint, a short scenario outline rather than business facts. "
        "Recover exact scenario conditions, exceptions, source URIs and full checks from assigned "
        "original Evidence, never from guessed details. Respect the fixed candidate boundary and, "
        "when supplied, case_blueprint; never add, rename or merge candidates at writing time. "
        "For capability-plan.json emit that exact object. "
        "For each candidate restrict guidance and cases to its planned task and boundary. "
        "BENCHMARK.md is a human-readable view that TE deterministically renders from validated benchmark.json; "
        "do not invent a different case set. Never generate full JSON a second time for the Markdown view.",
    )
    return (
        "\n## Evidence and tool contract\n\n"
        + compiler_execution_guidance().split("## Shared final-file case blueprint")[0].split(
            "## Evidence and tool contract\n\n", 1
        )[1]
        + "\n## Capability planning and bounded execution\n\n"
        "Use exactly the five-step plan below: one global capability planning Reduce followed by "
        "independent file-writing Map assignments. Maximum eight candidates, 36 files; "
        "do not use extra rounds, semantic routing, against=target or ready drafts. "
        "Keep the following Reduce stage restrictions in the runtime contract: records only, "
        "no ready_* fields or final content, role-specific optional fields and an 8000-token total "
        "planning target. Only synthesize writes files. "
        "Do not preselect a skill name or seven required_paths in the runtime planner: the Reduce "
        "chooses candidate boundaries and the concrete paths from Evidence. "
        "The number of candidates is determined by task boundaries, never document count. "
        "Zero candidates is a valid grounded result, not a publishable Skill library.\n\n"
        "```python\n" + COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )
