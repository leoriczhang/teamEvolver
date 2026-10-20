"""Keep generated Case history as local deduplication metadata, never scenario facts."""

from __future__ import annotations

import hashlib
import json

CASE_NARRATIVE_PROTOCOL = "te-source-bound-case-context.v1"
CASE_CONTEXT_BATCH_STAGE = "case_batch_source_bound"
CASE_CONTEXT_GROUP_STAGE = "case_group_source_bound"
CASE_NARRATIVE_RULE = (
    "Bind actors, roles, amounts and preferences to one original scenario; never merge customer facts across "
    "method/context inputs. Context adds no goal outside candidate boundaries. Keep query/materials/checks consistent. "
    "Prior generated text/feedback confirms no facts; unknowns stay unknown. Fingerprints are opaque dedup metadata."
)
CASE_COVERAGE_RULE = (
    "coverage_dimension is a preference, never a required domain condition. "
    "If an exception/refusal/numerical preference lacks support, choose another distinct scenario within "
    "the method's trigger/outcome using documented inputs, decisions or verification paths. "
    "Do not defer merely because the preferred dimension is absent. Defer only if no valid distinct in-boundary "
    "scenario with the required source-grounded checks is supported; never invent conditions or pad checks."
)


def source_bound_case_rules(rules: str) -> str:
    """Render a new version only; accepted compilers retain the original bytes."""
    start = rules.index("Use derived_controls.coverage_dimension")
    end = rules.index("The assigned derived_controls.candidate", start)
    rules = rules[:start] + CASE_COVERAGE_RULE + (
        " Whole previous queries stay local; the host rejects exact duplicates. "
        "Renaming actors or IDs is not a distinct scenario. "
    ) + rules[end:]
    rules = rules.replace("Example names and amounts are scenario inputs, not universal rules.", CASE_NARRATIVE_RULE)
    return rules + "\nCase context protocol: " + CASE_NARRATIVE_PROTOCOL + "."


def case_history_controls(cases: list[dict]) -> dict:
    """Preserve identity and whole-query fingerprints without exposing other customers' narratives."""
    return {
        "case_narrative_protocol": CASE_NARRATIVE_PROTOCOL,
        "case_narrative_rule": CASE_NARRATIVE_RULE,
        "scenario_binding_policy": "te-case-scenario-binding.v1",
        "actor_identity_policy": "te-case-role-identity.v1",
        "actor_identity_rule": "Do not give unnamed executors a customer name/alias. Keep source role mappings; "
        "an unknown identity stays unknown.",
        "previous_queries": [],
        "previous_queries_omitted": bool(cases),
        "previous_query_fingerprints": [
            {"case_id": row["case_id"], "query_sha256": hashlib.sha256(row["query"].encode("utf-8")).hexdigest()}
            for row in cases
        ],
        "complete_previous_cases_archived": True,
        "duplicate_queries_checked_by_host": True,
    }


def compact_history_controls(controls: dict) -> bool:
    """Drop only the optional per-Case projection when source and repair diagnostics need the budget."""
    fingerprints = controls.get("previous_query_fingerprints")
    if not fingerprints:
        if controls.pop("previous_query_fingerprints_sha256", None):
            return True  # Full historical Cases and their fingerprints remain computable from the local ledger.
        return False
    canonical = json.dumps(fingerprints, ensure_ascii=False, separators=(",", ":"))
    controls["previous_query_fingerprints_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    controls["previous_query_fingerprints_count"] = len(fingerprints)
    controls["previous_query_fingerprints_omitted"] = True
    controls["previous_query_fingerprints"] = []
    return True
