"""Finite, provenance-preserving default plan for the OV Skill Candidate compiler."""

import json

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
            "in this step."
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
            "not permission to omit any file or supporting input."
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
            "BENCHMARK.md must use the same case IDs and source-grounded checks."
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
