"""Frozen business-component editing tools for the shared Candidate editor loop.

These tools reuse the Skill exact-anchor patch kernel, not the Skill package
schema. All state is local to one Worker attempt; publication stays in the
existing Candidate pipeline. Evidence reads are complete unless the caller
explicitly requests a page, which is always labelled and hash-bound.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from collections import Counter

from team_replay.execution_plan import normalize_execution_plan
from team_replay.variable_context import inline_variable_binding, variable_context_groups
from team_skills.evolution.kernel.skill_edits import apply_skill_edits, format_edit_failures
from team_skills.execution.contracts import apply_changes
from team_skills.execution.contracts import editable_component_ids as configured_editable_ids
from team_skills.library.repository import SkillLibraryError

TOOLS = (
    "read_component", "read_evidence", "search_evidence", "propose_component_edits", "replace_component_content",
    "inspect_changes", "validate_changes",
)
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    raw = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def exact_refs(values, allowed):
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value or value not in allowed for value in values)):
        raise SkillLibraryError("original evidence_refs must be a nonempty exact allowed-reference list")
    return list(dict.fromkeys(values))


def _trace_ids(values):
    if not isinstance(values, list):
        raise SkillLibraryError("source Trace boundary must be a list")
    refs = [value.get("trace_id") if isinstance(value, dict) else value for value in values]
    if any(not isinstance(ref, str) or not ref for ref in refs):
        raise SkillLibraryError("source Trace boundary requires recorded Trace IDs")
    return set(refs)


def component_size_summary(published, working, staged=None):
    """Measure frozen raw text; these code-point/byte counts are not model Tokens."""
    staged = working if staged is None else staged
    published_parts = {part["component_id"]: part for part in published["components"]}
    staged_parts = {part["component_id"]: part for part in staged["components"]}
    if set(published_parts) != {part["component_id"] for part in working["components"]} or (
        set(staged_parts) != set(published_parts)
    ):
        raise SkillLibraryError("component size comparison requires the same frozen component identities")
    rows = []
    for part in working["components"]:
        cid = part["component_id"]
        texts = {"published": published_parts[cid]["content"], "working": part["content"],
                 "staged": staged_parts[cid]["content"]}
        row = {"component_id": cid}
        for basis, text in texts.items():
            row[basis + "_characters"] = len(text)
            row[basis + "_utf8_bytes"] = len(text.encode())
        for unit in ("characters", "utf8_bytes"):
            row["delta_vs_published_" + unit] = row["staged_" + unit] - row["published_" + unit]
            row["delta_vs_working_" + unit] = row["staged_" + unit] - row["working_" + unit]
        rows.append(row)
    numeric = set(rows[0]) - {"component_id"} if rows else set()
    return {"measurement": "raw_component_content_before_template_substitution",
            "character_unit": "unicode_code_points", "byte_unit": "utf8_bytes", "is_token_usage": False,
            "components": rows, "totals": {key: sum(row[key] for row in rows) for key in sorted(numeric)}}


def _select(value, path):
    if not isinstance(path, list):
        raise SkillLibraryError("path must be a list of exact JSON keys or array indexes")
    current = value
    for part in path:
        if isinstance(current, dict) and isinstance(part, str) and part in current:
            current = current[part]
        elif isinstance(current, list) and type(part) is int and 0 <= part < len(current):
            current = current[part]
        else:
            raise SkillLibraryError("Evidence path does not exist")
    return current


def _page(value, args):
    """Explicit character pages preserve a digest of the complete selected value."""
    path = args.get("path", [])
    selected = _select(value, path)
    text = canonical(selected)
    result = {
        "selection_path": copy.deepcopy(path), "document_sha256": sha(value),
        "full_sha256": sha(text), "full_utf8_bytes": len(text.encode()), "total_characters": len(text),
    }
    if "offset" not in args and "limit" not in args:
        return {**result, "complete": True, "has_more": False, "next_offset": None,
                "value": copy.deepcopy(selected)}
    offset, limit = args.get("offset", 0), args.get("limit", 8000)
    if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 131072:
        raise SkillLibraryError("Evidence page requires a valid character offset and limit in 1..131072")
    end = min(len(text), offset + limit)
    return {**result, "complete": offset == 0 and end == len(text), "offset": offset,
            "offset_units": "characters", "text": text[offset:end], "has_more": end < len(text),
            "next_offset": end if end < len(text) else None}


class PromptEditorTools:
    """Body-only mutations plus read-only access to frozen Evidence and metadata."""

    def __init__(self, payload, *, max_tool_calls_per_round=8, editable_component_ids=None):
        self.payload = copy.deepcopy(payload)
        self.basis = copy.deepcopy(payload["execution_plan"])
        self.baseline = copy.deepcopy(payload.get("baseline_execution_plan") or self.basis)
        self.working = copy.deepcopy(self.basis)
        normalize_execution_plan(self.basis)
        normalize_execution_plan(self.baseline)
        self.components = {part["component_id"]: part for part in self.basis["components"]}
        self.variable_groups = {group["group_id"]: group for group in variable_context_groups(self.basis)}
        self.available_tools = (*TOOLS, "read_variable_group") if self.variable_groups else TOOLS
        configured = configured_editable_ids(self.basis)
        requested = configured if editable_component_ids is None else set(editable_component_ids)
        if not requested <= self.components.keys():
            raise SkillLibraryError("editable components must belong to the frozen execution plan")
        self.editable = configured.intersection(requested)
        self.allowed_refs = set(payload["allowed_evidence_refs"])
        self.max_calls = max(1, int(max_tool_calls_per_round))
        self.plan_claims = {}
        self.diagnoses = []
        self.plan_history = []
        self.edit_provenance = []
        self.read_current = set()
        self.reasons = {}
        self.references = {}
        self.applied_edits = 0
        self.evidence_reads = []

    def accept_plan(self, plan, references, diagnoses, *, round_no):
        """Commit a validated plan without revoking earlier accepted edit support."""
        plan_id = "plan-" + str(len(self.plan_history) + 1)
        combined = {cid: set(refs) for cid, refs in self.plan_claims.items()}
        for cid, refs in references.items():
            combined.setdefault(cid, set()).update(refs)
        previous = copy.deepcopy(self.diagnoses)
        history = copy.deepcopy(self.plan_history)
        for diagnosis in previous:
            if diagnosis["component_id"] in references:
                diagnosis["analysis_status"] = "prior_accepted_hypothesis"
        for accepted_plan in history:
            for diagnosis in accepted_plan["diagnoses"]:
                if diagnosis["component_id"] in references:
                    diagnosis["analysis_status"] = "prior_accepted_hypothesis"
        accepted = [{**copy.deepcopy(diagnosis), "accepted_plan_id": plan_id, "accepted_round": round_no,
                     "analysis_status": "current_accepted_hypothesis"} for diagnosis in diagnoses]
        record = {"plan_id": plan_id, "round": round_no, "component_ids": sorted(references),
                  "rationale": plan["rationale"], "diagnoses": copy.deepcopy(accepted),
                  "hypotheses_are_not_verified_facts": True}
        self.plan_claims, self.diagnoses = combined, [*previous, *accepted]
        self.plan_history = [*history, record]
        return plan_id

    def _edit_support(self, cid, refs):
        support = []
        for ref in refs:
            plan_ids = list(dict.fromkeys(diagnosis["accepted_plan_id"] for diagnosis in self.diagnoses
                if diagnosis["component_id"] == cid and ref in diagnosis["evidence_refs"]))
            if not plan_ids:
                raise SkillLibraryError("patch reference has no accepted plan support for this component")
            support.append({"evidence_ref": ref, "accepted_plan_ids": plan_ids})
        return support

    def planned_diagnoses(self):
        """Only relevant accepted hypotheses accompany actual staged source support."""
        references = {change["component_id"]: set(change["evidence_refs"]) for change in self.changes()}
        return [copy.deepcopy(diagnosis) for diagnosis in self.diagnoses
                if references.get(diagnosis["component_id"], set()).intersection(diagnosis["evidence_refs"])]

    def _component(self, cid, *, version="current"):
        plan = self.working if version == "current" else self.baseline
        if version not in {"current", "baseline"}:
            raise SkillLibraryError("component version must be current or baseline")
        for part in plan["components"]:
            if part["component_id"] == cid:
                return part
        raise SkillLibraryError("unknown frozen business component")

    def read_component(self, args):
        if set(args) - {"component_id", "version"}:
            raise SkillLibraryError("unsupported read_component field")
        cid, version = args.get("component_id"), args.get("version", "current")
        part = self._component(cid, version=version)
        if version == "current":
            self.read_current.add(cid)
        return {"ok": True, "version": version, "component": copy.deepcopy(part),
                "editable": cid in self.editable, "content_characters": len(part["content"]),
                "content_utf8_bytes": len(part["content"].encode()), "content_sha256": sha(part["content"]),
                "metadata_read_only": True}

    def read_variable_group(self, args):
        if set(args) - {"group_id", "component_id", "version"}:
            raise SkillLibraryError("unsupported variable-group read field")
        group = self.variable_groups.get(args.get("group_id"))
        if group is None:
            raise SkillLibraryError("unknown frozen variable group")
        cid = args.get("component_id")
        if cid is not None:
            if cid not in {member["component_id"] for member in group["members"]}:
                raise SkillLibraryError("component is outside the selected variable group")
            return {**self.read_component({"component_id": cid, "version": args.get("version", "current")}),
                    "variable_group_id": group["group_id"]}
        version = args.get("version", "current")
        if version not in {"current", "baseline"}:
            raise SkillLibraryError("component version must be current or baseline")
        plan = self.working if version == "current" else self.baseline
        current = next(item for item in variable_context_groups(plan) if item["group_id"] == group["group_id"])
        return {"ok": True, "variable_group": current, "metadata_read_only": True,
                "editable_member_ids": sorted(self.editable.intersection(
                    member["component_id"] for member in current["members"])), "content_deferred": True}

    def _evidence_document(self, ref):
        if not isinstance(ref, str) or not ref:
            raise SkillLibraryError("read_evidence requires an exact recorded reference")
        records = []
        if ref in self.allowed_refs:
            for session in self.payload.get("evidence") or []:
                if str(session.get("session_id")) == ref:
                    records.append(copy.deepcopy(session))
                    continue
                for turn in session.get("turns") or []:
                    tid = str(turn.get("source_trace_id") or turn.get("trace_id") or "")
                    if tid == ref:
                        records.append({"session_id": session["session_id"],
                                        "provenance": copy.deepcopy(session.get("provenance") or {}),
                                        "recorded_turn": copy.deepcopy(turn)})
            if not records:
                raise SkillLibraryError("allowed Evidence reference has no recorded body")
            # Copies retain their source projections; they are not independent Evidence.
            distinct = {canonical(record): record for record in records}
            return {"ref": ref, "usage": "target", "records": list(distinct.values()),
                    "source_projections_are_not_new_evidence": True}
        for example in (self.payload.get("evidence_context") or {}).get("context_examples") or []:
            if ref == "context:" + str(example["example_id"]):
                return {"ref": ref, "usage": "context_only", "record": copy.deepcopy(example),
                        "eligible_as_evidence_ref": False, "eligible_as_case_anchor": False}
        raise SkillLibraryError("unknown Evidence reference; Context requires its explicit context:example_id")

    def target_refs(self, values):
        """Require an allowed reference to resolve to recorded target content."""
        refs = exact_refs(values, self.allowed_refs)
        for ref in refs:
            for record in self._evidence_document(ref)["records"]:
                provenance = record.get("provenance") or {}
                target_ids = _trace_ids(provenance.get("target_trace_refs") or [])
                context_ids = _trace_ids(provenance.get("context_trace_refs") or [])
                turns = [record["recorded_turn"]] if "recorded_turn" in record else record.get("turns") or []
                eligible = []
                for turn in turns:
                    identity = str(turn.get("source_trace_id") or turn.get("trace_id") or "")
                    if turn.get("context_only") or turn.get("usage") in {"context", "context_only"}:
                        continue
                    if "target_trace_refs" in provenance and identity not in target_ids:
                        continue
                    if "target_trace_refs" not in provenance and identity in context_ids:
                        continue
                    eligible.append(turn)
                legacy_messages = not turns and "target_trace_refs" not in provenance and record.get("messages")
                if record.get("context_only") or record.get("usage") in {"context", "context_only"} or not (
                    eligible or legacy_messages
                ):
                    raise SkillLibraryError("Evidence reference has no recorded current target content")
        return refs

    def component_target_refs(self, cid, values):
        refs = self.target_refs(values)
        if inline_variable_binding(self.components[cid]):
            applicable = (self.payload.get("component_evidence_refs") or {}).get(cid, [])
            if not set(refs) <= set(applicable):
                raise SkillLibraryError("Evidence does not activate this variable Context component")
        return refs

    def read_evidence(self, args):
        if set(args) - {"ref", "path", "offset", "limit"}:
            raise SkillLibraryError("unsupported read_evidence field")
        ref = args.get("ref")
        document = self._evidence_document(ref)
        result = _page(document, args)
        self.evidence_reads.append({"ref": ref, "usage": document["usage"],
                                    "full_sha256": result["full_sha256"], "complete": result["complete"],
                                    "selection_path": result["selection_path"]})
        return {"ok": True, "usage": document["usage"], **result}

    def search_evidence(self, args):
        if set(args) - {"query", "offset", "limit"}:
            raise SkillLibraryError("unsupported search_evidence field")
        query, offset, limit = args.get("query"), args.get("offset", 0), args.get("limit", 20)
        if (not isinstance(query, str) or not query or len(query) > 512 or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 100):
            raise SkillLibraryError("search requires query text, offset and a limit in 1..100")
        refs = list(dict.fromkeys(self.payload["allowed_evidence_refs"]))
        refs += ["context:" + item["example_id"] for item in
                 (self.payload.get("evidence_context") or {}).get("context_examples") or []]
        matches = []
        for ref in refs:
            document = self._evidence_document(ref)
            text = canonical(document)
            position = text.casefold().find(query.casefold())
            if position < 0:
                continue
            matches.append({"ref": ref, "usage": document["usage"], "document_sha256": sha(text),
                            "preview": text[max(0, position - 50):position + 110],
                            "preview_is_complete_evidence": False})
        end = min(len(matches), offset + limit)
        return {"ok": True, "matches": matches[offset:end], "total_matches": len(matches),
                "has_more": end < len(matches), "next_offset": end if end < len(matches) else None,
                "index_only": True}

    def replace_component_content(self, args):
        """Expand a Prompt-only body-hash macro through the existing exact editor."""
        if set(args) - {"component_id", "content", "expected_content_hash", "reason", "evidence_refs"}:
            raise SkillLibraryError("only component body, current content hash, reason and Evidence refs are writable")
        cid = args.get("component_id")
        if cid not in self.editable:
            raise SkillLibraryError("component is outside the frozen editable scope")
        if cid not in self.read_current:
            raise SkillLibraryError("read_component current before replacing the complete component")
        current = self._component(cid)["content"]
        if args.get("expected_content_hash") != sha(current):
            raise SkillLibraryError(
                "expected_content_hash must match current component content_sha256, not staged_hash",
            )
        if not isinstance(args.get("content"), str):
            raise SkillLibraryError("component content must be a string")
        return self.propose_component_edits({
            "component_id": cid, "reason": args.get("reason"), "evidence_refs": args.get("evidence_refs"),
            "edits": [{"operation": "replace", "old_string": current, "new_string": args["content"]}],
        })

    def propose_component_edits(self, args):
        if set(args) - {"component_id", "edits", "reason", "evidence_refs"}:
            raise SkillLibraryError("only body edits, reason and recorded Evidence references are writable")
        cid = args.get("component_id")
        if cid not in self.editable:
            raise SkillLibraryError("component is outside the frozen editable scope")
        if cid not in self.plan_claims:
            raise SkillLibraryError("a valid business-Prompt diagnosis plan is required before editing this component")
        if cid not in self.read_current:
            raise SkillLibraryError("read_component current content before copying exact edit anchors")
        refs = self.component_target_refs(cid, args.get("evidence_refs"))
        if not set(refs) <= self.plan_claims[cid]:
            raise SkillLibraryError("patch references must be supported by this component's accepted plans")
        support = self._edit_support(cid, refs)
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SkillLibraryError("a specific body-edit reason is required")
        edits = args.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict) and set(edit) - {"operation", "old_string", "new_string", "anchor"}:
                    raise SkillLibraryError("unsupported exact body-edit field")
        old = self._component(cid)["content"]
        applied = apply_skill_edits(old, edits)
        if not applied.ok:
            raise SkillLibraryError(format_edit_failures(applied.failures))
        if not inline_variable_binding(self.components[cid]) and Counter(_TEMPLATE.findall(applied.content)) != Counter(
            _TEMPLATE.findall(self.components[cid]["content"])
        ):
            raise SkillLibraryError("template variable names and occurrence counts must remain unchanged")
        combined_reasons = [*self.reasons.get(cid, []), reason]
        combined_refs = list(dict.fromkeys([*self.references.get(cid, []), *refs]))
        proposed = {"component_id": cid, "content": applied.content,
                    "reason": "\n".join(dict.fromkeys(combined_reasons)), "evidence_refs": combined_refs}
        cumulative = [change for change in self.changes() if change["component_id"] != cid]
        if applied.content != self.components[cid]["content"]:
            cumulative.append(proposed)
        if applied.content == old:
            raise SkillLibraryError("exact edits made no content change")
        # A staged composition is always one proposed revision over the frozen
        # basis, regardless of how many tool rounds built it.
        updated = (apply_changes(self.basis, cumulative, allowed_modules=["prompt"])
                   if cumulative else copy.deepcopy(self.basis))
        normalize_execution_plan(updated)  # Includes actual template rendering and expansion limits.
        self.working = updated
        self.reasons[cid] = combined_reasons
        self.references[cid] = combined_refs
        self.applied_edits += applied.applied
        self.edit_provenance.append({"component_id": cid, "evidence_refs": list(refs), "support": support,
                                     "before_content_sha256": sha(old), "after_content_sha256": sha(applied.content),
                                     "applied_edits": applied.applied})
        return {"ok": True, "component_id": cid, "applied": applied.applied,
                "before_characters": len(old), "after_characters": len(applied.content),
                "character_delta": len(applied.content) - len(old), "content_sha256": sha(applied.content),
                "diff": "".join(difflib.unified_diff(old.splitlines(True), applied.content.splitlines(True),
                                                     fromfile="before", tofile="staged")),
                "mechanical_only": True}

    def changes(self):
        result = []
        for cid in self.components:
            content = self._component(cid)["content"]
            if content == self.components[cid]["content"]:
                continue
            result.append({"component_id": cid, "content": content,
                           "reason": "\n".join(dict.fromkeys(self.reasons.get(cid, []))),
                           "evidence_refs": list(self.references.get(cid, []))})
        return result

    def inspect_changes(self, args):
        if args:
            raise SkillLibraryError("inspect_changes accepts no fields")
        items = []
        for change in self.changes():
            cid = change["component_id"]
            before, after = self.components[cid]["content"], change["content"]
            items.append({"component_id": cid, "before_characters": len(before), "after_characters": len(after),
                          "character_delta": len(after) - len(before), "reason": change["reason"],
                          "evidence_refs": change["evidence_refs"],
                          "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                               fromfile="frozen-working-basis", tofile="staged"))})
        return {"ok": True, "changes": items, "staged_hash": sha(self.working),
                "component_size_summary": component_size_summary(self.baseline, self.basis, self.working),
                "accepted_plan_ids": [record["plan_id"] for record in self.plan_history],
                "edit_provenance": copy.deepcopy(self.edit_provenance),
                "mechanical_only": True}

    def mechanical_validation(self):
        changes = self.changes()
        if not changes:
            raise SkillLibraryError("no staged component content changed")
        checked = apply_changes(self.basis, changes, allowed_modules=["prompt"])
        normalize_execution_plan(checked)
        if checked != self.working:
            raise SkillLibraryError("staged composition differs from the frozen exact-edit materialization")
        for change in changes:
            cid = change["component_id"]
            if cid not in self.editable or cid not in self.plan_claims:
                raise SkillLibraryError("staged component no longer has an authorized diagnosis")
            self.component_target_refs(cid, change["evidence_refs"])
            if not set(change["evidence_refs"]) <= self.plan_claims[cid]:
                raise SkillLibraryError("staged Evidence references differ from accepted component plans")
            supported = {ref for edit in self.edit_provenance if edit["component_id"] == cid
                         for ref in edit["evidence_refs"]}
            if not set(change["evidence_refs"]) <= supported:
                raise SkillLibraryError("staged Evidence references have no successful authorized patch provenance")
            if not inline_variable_binding(self.components[cid]) and Counter(
                _TEMPLATE.findall(change["content"])
            ) != Counter(
                _TEMPLATE.findall(self.components[cid]["content"])
            ):
                raise SkillLibraryError("template variables changed")
        return checked

    def validate_changes(self, args):
        if args:
            raise SkillLibraryError("validate_changes accepts no fields")
        self.mechanical_validation()
        return {"ok": True, "scope": "body_scope_references_template_and_plan_contract",
                "mechanical_only": True, "mechanical_passed": True, "staged_hash": sha(self.working)}

    async def execute(self, calls):
        if not isinstance(calls, list):
            return [{"ok": False, "error": "tool_calls must be a list"}]
        result = []
        if len(calls) > self.max_calls:
            result.append({"tool": "(limiter)", "ok": False, "error": "too many tools in one editor round"})
        for call in calls[:self.max_calls]:
            if not isinstance(call, dict) or set(call) - {"tool", "args"}:
                result.append({"ok": False, "error": "each tool call requires tool and args only"})
                continue
            name, args = call.get("tool"), call.get("args", {})
            if name not in self.available_tools or not isinstance(args, dict):
                result.append({"tool": name, "ok": False, "error": "unknown tool or non-object arguments",
                               "available_tools": list(self.available_tools)})
                continue
            try:
                value = getattr(self, name)(args)
                result.append({"tool": name, **value})
            except Exception as exc:  # An edit error is a correction observation, never a partial publish.
                result.append({"tool": name, "ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return result
