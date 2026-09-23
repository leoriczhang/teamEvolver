"""Tenant-bound True Replay orchestration; customer execution stays in factories."""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from pathlib import Path
from typing import Any, Mapping, Optional

from ._util import stable_hash
from .adapter_runtime import AdapterError
from .artifacts import skill_treatment_members, validate_skill_treatment
from .execution import run_branch
from .hooks import ReplayAdapterFactory, ReplayContext, ReplayTreatment, ReplayUnsupported
from .host import current_host
from .metrics import branch_efficiency, compare_efficiency
from .policy import initial_query, normalize_case_checklist, normalize_checklist_report, progressive_config, progressive_replay_decision


def load_candidate_job(job_id: str) -> dict[str, Any] | None:
    return current_host().load_candidate_job(job_id) or None


def load_candidate_job_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("job file must contain a JSON object")
    value.setdefault("job_id", Path(path).stem)
    return value


def load_source_session(session_id: str) -> dict[str, Any] | None:
    host = current_host(required=False)
    return host.load_source_session(session_id) if host and session_id else None


def load_context_snapshot(snapshot_id: str, source_session: Mapping[str, Any]) -> dict[str, Any] | None:
    host = current_host(required=False)
    return host.load_context_snapshot(snapshot_id, source_session) if host and snapshot_id else None


def source_runtime_type(source_session: Mapping[str, Any]) -> str:
    runtime = source_session.get("runtime") or {}
    return str(runtime.get("type") or source_session.get("source") or "").strip().lower()


def resolve_factory(source_session: Mapping[str, Any]) -> ReplayAdapterFactory:
    host = current_host(required=False)
    factory = host.resolve_replay_factory(source_runtime_type(source_session), source_session) if host else None
    if factory is None:
        raise ReplayUnsupported("No available Replay adapter bound to this tenant")
    return factory


def read_team_evolver_harness() -> dict[str, Any]:
    host = current_host(required=False)
    return host.judge_harness() if host else {}


def _snapshot(case: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(case.get("context_snapshot"), dict):
        return copy.deepcopy(case["context_snapshot"])
    snapshot_id = str(case.get("context_snapshot_id") or "")
    if not snapshot_id:
        turn_num = int(case.get("turn_num") or 0)
        turn = next((turn for turn in source.get("turns") or [] if turn.get("turn_num") == turn_num), {})
        snapshot_id = str((turn.get("context_usage") or {}).get("context_snapshot_id") or "")
    if not snapshot_id:
        return {}
    snapshot = load_context_snapshot(snapshot_id, source)
    if snapshot is None:
        raise ReplayUnsupported("Recorded Context snapshot is unavailable to this tenant/user")
    return copy.deepcopy(snapshot)


def make_context(
    branch: str, skill: Mapping[str, Any] | None, case: Mapping[str, Any],
    source: Mapping[str, Any], timeout: int, *, snapshot: Mapping[str, Any] | None = None,
) -> ReplayContext:
    return ReplayContext(
        request_id="replay_" + uuid.uuid4().hex,
        runtime_type=source_runtime_type(source),
        treatment=ReplayTreatment(branch, copy.deepcopy(skill)),
        materials=tuple(copy.deepcopy(case.get("materials") or [])),
        context_snapshot=copy.deepcopy(snapshot) if snapshot is not None else _snapshot(case, source),
        timeout_seconds=max(1, min(1800, int(timeout))),
    )


def treatment_for_case(
    treatment: Mapping[str, Any] | None,
    case: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Select the peer Skill subset explicitly referenced by a Replay Case."""
    members = skill_treatment_members(treatment)
    requested = {
        str(item or "").strip()
        for item in case.get("skill_ids") or []
        if str(item or "").strip()
    }
    if not requested or not members:
        return treatment
    selected = [
        member
        for member in members
        if str(member.get("name") or "") in requested
    ]
    found = {str(member.get("name") or "") for member in selected}
    missing = sorted(requested - found)
    if missing:
        raise ReplayUnsupported(
            "Replay Case references unavailable Skills: " + ", ".join(missing)
        )
    if len(selected) == 1:
        return selected[0]
    return {"kind": "skill_set", "skills": selected}


def execute_pair(
    factory: ReplayAdapterFactory, contexts: Mapping[str, ReplayContext], case: Mapping[str, Any],
    *, harness: Mapping[str, Any], max_interactions: int,
) -> dict[str, dict[str, Any]]:
    """Pin one factory revision and open both independent sessions before execution."""
    sessions = {}
    try:
        for branch in ("baseline", "candidate"):
            session = factory.open(copy.deepcopy(contexts[branch]))
            if any(session is existing for existing in sessions.values()):
                raise ReplayUnsupported("factory reused a Session across A/B branches")
            if not callable(getattr(session, "send", None)) or not callable(getattr(session, "close", None)):
                raise TypeError("factory must return a Session with send and close")
            sessions[branch] = session
    except Exception as exc:
        for session in sessions.values():
            try:
                session.close()
            except Exception:
                pass
        return {branch: {
            "branch": branch, "ok": False, "status": "unsupported", "error_code": "REPLAY_UNSUPPORTED",
            "error": str(exc), "checklist_report": {"judge": "unavailable", "all_satisfied": False},
        } for branch in contexts}
    results = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="replay-branch") as pool:
        futures = {
            pool.submit(copy_context().run, run_branch, factory, contexts[branch], copy.deepcopy(case),
                        harness=copy.deepcopy(harness), max_interactions=max_interactions,
                        session=sessions[branch]): branch
            for branch in ("baseline", "candidate")
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def spawn_native_agent_branch(
    branch: str, instruction: str, skill: dict[str, Any] | None, job: dict[str, Any],
    case: dict[str, Any], source_session: dict[str, Any], timeout: int, max_interactions: int = 4,
    *, factory: ReplayAdapterFactory | None = None,
) -> dict[str, Any]:
    """Compatibility entry point; all execution goes through the factory contract."""
    del job
    case = {**case, "query": instruction}
    context = make_context(branch, skill, case, source_session, timeout)
    return run_branch(factory or resolve_factory(source_session), context, case,
                      harness=read_team_evolver_harness(), max_interactions=max_interactions)

def extract_referenced_paths(text: str) -> list[str]:
    """Pull filesystem-path-looking tokens out of a free-text instruction.

    Catches absolute paths (/home/...) and repo-relative hints (teamEvolver/...,
    integrations/...). Intentionally loose — grounding is advisory."""
    import re

    tokens = re.split(r"[\s,;，；、]+", text.strip())
    hits: list[str] = []
    for tok in tokens:
        tok = tok.strip().strip("`'\"()[]{}<>。，！？：:")
        if not tok:
            continue
        looks_pathy = (
            tok.startswith("/")
            or ("/" in tok and not tok.startswith(("http://", "https://")))
            or bool(
                re.search(
                    r"\.(?:zip|tar|gz|tgz|7z|pdf|docx?|xlsx?|pptx?|csv|json|ya?ml|md|txt|html?)$",
                    tok,
                    re.IGNORECASE,
                )
            )
        )
        if looks_pathy:
            hits.append(tok)
    return list(dict.fromkeys(hits))


def check_paths(paths: list[str], search_roots: list[Path]) -> list[dict[str, Any]]:
    """Resolve each referenced path (absolute, or relative to any search root)
    and report whether it exists on this machine."""
    out: list[dict[str, Any]] = []
    for p in paths:
        resolved: Optional[str] = None
        exists = False
        cand = Path(p)
        if cand.is_absolute():
            exists = cand.exists()
            resolved = str(cand) if exists else None
        else:
            for root in search_roots:
                probe = (root / p)
                if probe.exists():
                    exists, resolved = True, str(probe)
                    break
        out.append({"path": p, "exists": exists, "resolved": resolved})
    return out


def _uploaded_material_path(
    referenced_path: str,
    materials: list[dict[str, Any]],
) -> Optional[str]:
    wanted = str(referenced_path or "").strip().strip("`'\"").replace("\\", "/")
    wanted = wanted.rstrip("/")
    if not wanted or wanted.startswith("/"):
        return None
    for item in materials:
        material_path = str(item.get("path") or "").strip().replace("\\", "/")
        if (
            material_path == wanted
            or material_path.startswith(f"{wanted}/")
            or material_path.rsplit("/", 1)[-1] == wanted
        ):
            return material_path
    return None


def annotate_cases(job: dict[str, Any], search_roots: list[Path]) -> list[dict[str, Any]]:
    """Attach path-grounding to every replay case and flag which are runnable."""
    cases = []
    for idx, case in enumerate(job.get("replay_cases") or []):
        instr = initial_query(case)
        checklist = normalize_case_checklist(case)
        disclosure = progressive_config(case)
        materials = [
            dict(item)
            for item in (case.get("materials") or [])
            if isinstance(item, dict) and item.get("path")
        ]
        refs = check_paths(extract_referenced_paths(instr), search_roots)
        for ref in refs:
            uploaded = _uploaded_material_path(str(ref.get("path") or ""), materials)
            if uploaded:
                ref.update(
                    {
                        "exists": True,
                        "resolved": f"uploaded://{uploaded}",
                    }
                )
        referenced = list(refs)
        missing = [r for r in referenced if not r["exists"]]
        # Runnable when the instruction either references no path, or every
        # referenced path resolves on this machine.
        runnable = len(missing) == 0
        cases.append(
            {
                **case,
                "index": idx,
                "session_id": case.get("session_id"),
                "turn_num": case.get("turn_num"),
                "instruction": instr,
                "query": instr,
                "evidence_window": str(
                    case.get("evidence_window") or "recent"
                ),
                "evaluation_profile": str(
                    case.get("evaluation_profile") or ""
                ),
                "source_runtime": (
                    dict(case.get("source_runtime"))
                    if isinstance(case.get("source_runtime"), dict)
                    else {}
                ),
                "source_runtime_context": (
                    dict(case.get("source_runtime_context"))
                    if isinstance(case.get("source_runtime_context"), dict)
                    else {}
                ),
                "context_snapshot_id": str(
                    case.get("context_snapshot_id") or ""
                ),
                "had_tool_calls": bool(case.get("had_tool_calls")),
                "gold": case.get("gold") if isinstance(case.get("gold"), dict) else {},
                "requirements": case.get("requirements") or [],
                "trajectory_requirements": (
                    case.get("trajectory_requirements") or []
                ),
                "checklist": checklist,
                "progressive_disclosure": disclosure,
                "materials": materials,
                "target_dimensions": case.get("target_dimensions") or [],
                "difficulty": str(case.get("difficulty") or ""),
                "referenced_paths": referenced,
                "missing_paths": missing,
                "grounded": bool(referenced) and runnable,
                "runnable": runnable,
            }
        )
    return cases



def render_trajectory(messages: list[dict[str, Any]]) -> str:
    """Render an OpenAI-format message list into a numbered tool-call trace,
    the same evidence style agent_evolve_evaluation feeds its trajectory judge."""
    lines: list[str] = []
    step = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                step += 1
                fn = (tc.get("function") or {})
                args = fn.get("arguments")
                if isinstance(args, str) and len(args) > 4000:
                    args = args[:4000] + "…"
                lines.append(f"[step {step}] call {fn.get('name')}({args})")
        elif role == "tool":
            content = m.get("content")
            if isinstance(content, str) and len(content) > 4000:
                content = content[:4000] + "…"
            lines.append(f"        ↳ result: {content}")
    return "\n".join(lines) if lines else "(no tool calls were made)"



def _evaluate_case(job_id, job, case, source, factory, timeout, max_interactions):
    snapshot = _snapshot(case, source)
    contexts = {
        branch: make_context(
            branch,
            treatment_for_case(
                job.get("current_skill")
                if branch == "baseline"
                else job["candidate_skill"],
                case,
            ),
            case,
            source,
            timeout,
            snapshot=snapshot,
        )
        for branch in ("baseline", "candidate")
    }
    results = execute_pair(factory, contexts, case, harness=read_team_evolver_harness(), max_interactions=max_interactions)
    checklists = {branch: normalize_checklist_report(result, expected_checklist=case["checklist"])
                  for branch, result in results.items()}
    efficiency = compare_efficiency(results["baseline"], results["candidate"])
    policy = progressive_replay_decision(efficiency=efficiency, baseline_checklist=checklists["baseline"],
                                         candidate_checklist=checklists["candidate"])
    failures = [f"{branch}: {result.get('error', 'branch failed')}" for branch, result in results.items() if not result.get("ok")]
    if failures:
        policy.update(accepted=False, verdict="inconclusive", no_regression=False, decision_basis="branch_failure")
    branches = {
        branch: {**result, "checklist_report": checklists[branch], "instruction": case["query"],
                 "session_id": case.get("session_id"), "turn_num": case.get("turn_num"),
                 "trajectory": render_trajectory(result.get("messages") or [])}
        for branch, result in results.items()
    }
    return {
        "status": "failed" if failures else "evaluated", "mode": "true_replay", "job_id": job_id,
        "accepted": policy["accepted"], "verdict": policy["verdict"], "no_regression": policy["no_regression"],
        "decision_policy": policy, "efficiency": efficiency, "checklist": checklists,
        "reason": "; ".join(failures) or policy.get("decision_basis", ""),
        "adapter_revision": getattr(factory, "revision", ""),
        "runtime_type": source_runtime_type(source), "case_count": 1, "case": {"index": case["index"]},
        "max_interactions": max_interactions, "progressive_disclosure": case["progressive_disclosure"],
        "cases": [branches],
    }


def evaluate_job(
    job_id: str, *, job: dict[str, Any] | None = None, case_index: int | None = None,
    timeout: int = 600, keep_sandbox: bool = False, max_interactions: int = 4,
) -> dict[str, Any]:
    del keep_sandbox  # Factory sessions own cleanup, including on failure.
    job = copy.deepcopy(job if job is not None else load_candidate_job(job_id))
    if job is None:
        return {"status": "not_found", "job_id": job_id}
    skill = job.get("candidate_skill") or {}
    host = current_host(required=False)
    validation = host.validate_skill_treatment(skill) if host else validate_skill_treatment(skill)
    if not validation.get("passed"):
        return {"status": "evaluated", "mode": "true_replay", "job_id": job_id, "accepted": False,
                "verdict": "reject", "no_regression": False, "static_validation": validation,
                "reason": "candidate bundle failed deterministic static checks", "cases": []}
    # Files belong to the dataset or downstream runtime. Never infer availability from the server HOME.
    cases = [case for case in annotate_cases(job, []) if case["query"] and (case_index is None or case["index"] == case_index)]
    if not cases:
        return {"status": "skipped", "mode": "true_replay", "job_id": job_id, "accepted": False,
                "verdict": "inconclusive", "reason": "no matching Test Dataset", "cases": []}
    outputs = []
    factory = None
    for case in cases:
        try:
            source = copy.deepcopy(load_source_session(str(case.get("session_id") or "")) or {})
            source.setdefault("session_id", case.get("session_id"))
            source["runtime"] = {**case.get("source_runtime", {}), **source.get("runtime", {})}
            source["runtime_context"] = {**case.get("source_runtime_context", {}), **source.get("runtime_context", {})}
            if source.get("source") == "managed_agent_candidate_audit" or source["runtime_context"].get("candidate_job_id"):
                raise ReplayUnsupported("candidate-audit sessions cannot be Replay sources")
            if not source_runtime_type(source):
                raise ReplayUnsupported("Test Dataset must specify source_runtime.type")
            if factory is None:
                factory = resolve_factory(source)
            output = _evaluate_case(job_id, job, case, source, factory, timeout, max(1, min(20, int(max_interactions))))
        except Exception as exc:
            output = {"status": "skipped" if isinstance(exc, (ReplayUnsupported, AdapterError)) else "failed",
                      "mode": "true_replay", "accepted": False, "verdict": "inconclusive", "no_regression": False,
                      "job_id": job_id, "reason": str(exc), "case_count": 0, "cases": []}
        outputs.append((str(case["index"]), output))
    if len(outputs) == 1:
        return outputs[0][1]
    from .aggregation import aggregate_true_replay_windows
    return {**aggregate_true_replay_windows(outputs), "job_id": job_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id")
    parser.add_argument("--job-file")
    parser.add_argument("--case", type=int)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-interactions", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--adapter-file", help="Owner-managed Python factory for standalone execution")
    parser.add_argument("--adapter-config", help="JSON configuration for the standalone factory")
    args = parser.parse_args()
    if not args.job_id and not args.job_file:
        parser.error("--job-id or --job-file is required")
    if args.adapter_file:
        from types import SimpleNamespace
        from .adapter_runtime import build_from_content
        from .host import configure_host
        config = json.loads(Path(args.adapter_config).read_text()) if args.adapter_config else {}
        path = Path(args.adapter_file)
        factory = build_from_content(path.read_text(), path.name, SimpleNamespace(**config), tenant_id="standalone")

        class StandaloneHost:
            def resolve_replay_factory(self, runtime_type, source_session): return factory
            def load_source_session(self, session_id): return None
            def load_context_snapshot(self, snapshot_id, source_session): return None
            def validate_skill_treatment(self, skill): return validate_skill_treatment(skill)
            def judge_harness(self): return dict(config.get("judge") or {})
        configure_host(StandaloneHost())
    job = load_candidate_job_file(args.job_file) if args.job_file else load_candidate_job(args.job_id)
    result = ({"cases": annotate_cases(job, []), "dry_run": True} if args.dry_run else
              evaluate_job(args.job_id or job["job_id"], job=job, case_index=args.case,
                           timeout=args.timeout, max_interactions=args.max_interactions))
    if args.json:
        print("TRUE_REPLAY_JSON_BEGIN")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        print("TRUE_REPLAY_JSON_END")


if __name__ == "__main__":
    main()
