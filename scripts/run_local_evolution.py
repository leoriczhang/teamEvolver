"""Run the skill-evolution pipeline fully locally (no OpenViking).

All object storage (session queue, archive, ledger, skill bundles, validation
candidates) lives in a LocalObjectStore under the run directory.

Phases:
  1. ingest  — value-classify every converted session (LLM, bounded
     concurrency) and queue/skip it through SessionStore
  2. evolve  — run EvolveServer cycles until the queue is drained
  3. export  — per-session analysis CSV + skill-evolution summary CSV

Usage:
    .venv/bin/python scripts/run_local_evolution.py                 # full run
    .venv/bin/python scripts/run_local_evolution.py --export-only   # re-export CSVs
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path

TEAM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TEAM_ROOT))

SESSIONS_DIR_DEFAULT = TEAM_ROOT.parent / "openclaw_sessions_converted"
RUNS_ROOT = TEAM_ROOT.parent


# ------------------------------------------------------------------------- #
# Phase 0: seed current skills from the OpenViking team library              #
# ------------------------------------------------------------------------- #

def seed_current_skills(store_root: Path) -> dict:
    """Copy the team's current skill library from OpenViking into the local
    store so the evolve engine has real baselines for ``_fetch_skill_bundle``.

    Without this, candidates are planned against an empty baseline: the
    registry only carries identity rows and ``skills/<name>/SKILL.md`` is
    missing, which is exactly the "current skills never loaded" failure of
    earlier local runs. Uses exact-key gets (prefix scans on OpenViking are
    extremely slow) driven by the manifest's file lists.
    """
    from teamEvolver.config_store import ConfigStore
    from teamEvolver.storage import build_object_store

    cfg = ConfigStore().to_config()
    endpoint = str(cfg.sharing_viking_endpoint or "").strip()
    api_key = str(cfg.sharing_viking_team_api_key or cfg.sharing_viking_api_key or "")
    if not endpoint or not api_key:
        print("[seed] viking endpoint/key not configured; skipping skill seed", flush=True)
        return {"seeded": 0, "reason": "not_configured"}

    remote = build_object_store(
        backend="viking",
        endpoint=endpoint,
        viking_account="default",
        viking_user="wrf",
        viking_agent="team-skill-evolver",
        viking_api_key=api_key,
        viking_root_prefix="team-skill-evolver",
        viking_namespace="resources",
        allow_fallback=False,
    )
    local = build_object_store(backend="local", local_root=str(store_root))

    def _get(key: str) -> bytes | None:
        try:
            return remote.get_object(key).read()
        except Exception:
            return None

    manifest_raw = _get("manifest.json")
    if not manifest_raw:
        print("[seed] no manifest.json in team library; nothing to seed", flush=True)
        return {"seeded": 0, "reason": "no_manifest"}

    skills: dict[str, dict] = {}
    for line in manifest_raw.decode("utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("name"):
            skills[str(rec["name"])] = rec

    copied = 0
    for name, rec in sorted(skills.items()):
        for item in rec.get("files") or []:
            rel = str((item or {}).get("path") or "").strip()
            if not rel:
                continue
            key = f"skills/{name}/SKILL.md" if rel == "SKILL.md" else f"skills/{name}/files/{rel}"
            data = _get(key)
            if data is not None:
                local.put_object(key, data)
                copied += 1
        # Version bundles (baseline for candidate validation / version views).
        # Probe v1..vN via the version record key; stop at the first miss.
        version = 1
        while version <= 20:
            record_key = f"skills/{name}/versions/v{version}/bundle.json"
            record = _get(record_key)
            if record is None:
                break
            local.put_object(record_key, record)
            copied += 1
            try:
                version_files = json.loads(record).get("files") or []
            except json.JSONDecodeError:
                version_files = []
            for item in version_files:
                rel = str((item or {}).get("path") or "").strip()
                if not rel:
                    continue
                key = (
                    f"skills/{name}/versions/v{version}/SKILL.md"
                    if rel == "SKILL.md"
                    else f"skills/{name}/versions/v{version}/files/{rel}"
                )
                data = _get(key)
                if data is not None:
                    local.put_object(key, data)
                    copied += 1
            version += 1

    local.put_object("manifest.json", manifest_raw)
    registry = _get("evolve_skill_registry.json")
    if registry is not None:
        local.put_object("evolve_skill_registry.json", registry)
    print(f"[seed] seeded {len(skills)} skills / {copied} objects from OpenViking", flush=True)
    return {"seeded": len(skills), "objects": copied}


# ------------------------------------------------------------------------- #
# Phase 1: ingest (classify + queue)                                        #
# ------------------------------------------------------------------------- #

async def run_ingest(cfg, sessions_dir: Path, run_dir: Path, concurrency: int) -> dict:
    """Classify AND judge every non-empty session; queue only valuable ones.

    Judging is deliberately decoupled from the evolution trigger: every
    session with meaningful content goes through summarize + session-judge so
    downstream consumers get scores for the full population, while only
    ``valuable`` sessions enter the evolution queue. Queued sessions carry
    ``_judge_scores`` so the evolve cycle's judge stage is a cache hit
    (``_should_skip_judging`` respects pre-existing scores).
    """
    from teamEvolver.evolve.stages.judge import judge_has_defect_evidence, judge_session
    from teamEvolver.evolve.stages.summarize import (
        _extract_session_metadata,
        build_session_trajectory,
        summarize_session,
    )
    from teamEvolver.integrations.langfuse_pull import (
        _has_meaningful_content,
        sanitize_session_id,
    )
    from teamEvolver.llm import AsyncLLMClient
    from teamEvolver.session_filter import SessionValueClassifier
    from teamEvolver.session_store import SessionStore

    store = SessionStore.from_config(cfg)
    classifier = SessionValueClassifier.from_config(cfg)

    # LLM client for summarize + judge, mirroring EvolveServerConfig's
    # resolution so scores match what the evolve pipeline would produce.
    llm = AsyncLLMClient(
        api_key=str(getattr(cfg, "llm_api_key", "") or ""),
        base_url=str(
            getattr(cfg, "llm_api_base", "")
            or "https://ark.cn-beijing.volces.com/api/v3"
        ),
        model=os.environ.get(
            "EVOLVE_MODEL", str(getattr(cfg, "llm_model_id", "") or "")
        ),
        max_tokens=int(
            os.environ.get(
                "EVOLVE_LLM_MAX_TOKENS",
                str(getattr(cfg, "llm_max_tokens", 100000) or 100000),
            )
        ),
        temperature=float(
            os.environ.get(
                "EVOLVE_LLM_TEMPERATURE",
                str(getattr(cfg, "llm_temperature", 0.4)),
            )
        ),
    )

    # Resumable judge ledger: sid -> scores dict (incremental flush per chunk).
    judge_path = run_dir / "judge_results.json"
    judge_results: dict[str, dict] = {}
    if judge_path.exists():
        loaded = _load_json(judge_path)
        if isinstance(loaded, dict):
            judge_results = {
                str(k): v
                for k, v in loaded.items()
                if isinstance(v, dict)
            }

    files = sorted(f for f in sessions_dir.glob("session_*.json"))
    counts = {
        "queued": 0, "skipped": 0, "requeued": 0,
        "empty": 0, "duplicate": 0, "error": 0, "judged": 0,
    }
    rows: list[dict] = []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def classify(session: dict) -> dict:
        async with sem:
            return await classifier.classify(session)

    async def summarize_and_judge(session: dict) -> None:
        """Attach trajectory/summary then judge; scores land on the session."""
        sid = sanitize_session_id(session.get("session_id") or "")
        if sid and sid in judge_results:
            # already judged in a previous (interrupted) run — reuse the score
            session["_judge_scores"] = judge_results[sid]
            return
        _extract_session_metadata(session)
        session["_trajectory"] = build_session_trajectory(session)
        async with sem:
            session["_summary"] = await summarize_session(llm, session)
            await judge_session(llm, session)

    chunk = 16
    started = time.monotonic()
    for offset in range(0, len(files), chunk):
        batch_files = files[offset : offset + chunk]
        payloads: list[dict] = []
        for f in batch_files:
            try:
                payloads.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                counts["error"] += 1
                rows.append({"file": f.name, "status": "error", "reason": repr(exc)})
                payloads.append(None)

        judges = await asyncio.gather(
            *(
                classify(p) if p and _has_meaningful_content(p) else _nowait(None)
                for p in payloads
            )
        )
        # Judge EVERY non-empty session — valuable or not — before saving.
        await asyncio.gather(
            *(
                summarize_and_judge(p) if p and _has_meaningful_content(p) else _nowait(None)
                for p in payloads
            )
        )

        for f, payload, judge in zip(batch_files, payloads, judges):
            if payload is None:
                continue
            sid = sanitize_session_id(payload.get("session_id") or f.stem)
            payload["session_id"] = sid
            if not str(payload.get("user_alias") or "").strip():
                payload["user_alias"] = "unknown"
            if judge is None:
                counts["empty"] += 1
                rows.append({"session_id": sid, "status": "empty"})
                continue
            try:
                scores = payload.get("_judge_scores")
                if isinstance(scores, dict) and scores.get("overall_score") is not None:
                    judge_results[sid] = scores
                    counts["judged"] += 1
                    # Persist the review scores on the session itself so the
                    # session index / filter audit rows carry them: skipped
                    # sessions never enter an evolution cycle, and only cycle
                    # history used to expose judge conclusions — which left
                    # most of the population without a visible review result.
                    payload["judge"] = {
                        key: scores[key]
                        for key in (
                            "overall_score",
                            "task_completion",
                            "response_quality",
                            "efficiency",
                            "tool_usage",
                            "evolution_evidence",
                            "evidence_reason",
                            "reasons",
                            "rationale",
                        )
                        if scores.get(key) is not None
                    }
                if store.duplicate_of_processed(payload):
                    counts["duplicate"] += 1
                    rows.append({"session_id": sid, "status": "duplicate"})
                    continue
                payload["value_judge"] = judge
                payload["ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                # Keep only the judge cache; the summarize stage's metadata
                # keys include sets (not JSON serializable) and are recomputed
                # by the evolve cycle anyway.
                for key in list(payload):
                    if key.startswith("_") and key != "_judge_scores":
                        del payload[key]
                if judge.get("decision") == "valuable":
                    store.save_queued(payload)
                    counts["queued"] += 1
                    rows.append({"session_id": sid, "status": "queued"})
                elif judge_has_defect_evidence(scores):
                    # The value filter only recognizes positive reusable
                    # evidence; a failing session (judge below the defect
                    # threshold, or an explicit defect flag) is exactly the
                    # badcase material evolution needs, so re-queue it instead
                    # of archiving it as skipped. The original classifier
                    # decision is preserved for the CSV/audit trail.
                    payload["requeue_reason"] = "judge_defect_evidence"
                    store.save_queued(payload)
                    counts["requeued"] += 1
                    rows.append(
                        {
                            "session_id": sid,
                            "status": "queued",
                            "decision": judge.get("decision"),
                            "requeue_reason": "judge_defect_evidence",
                        }
                    )
                else:
                    store.save_skipped(payload)
                    counts["skipped"] += 1
                    rows.append(
                        {"session_id": sid, "status": "skipped", "decision": judge.get("decision")}
                    )
            except Exception as exc:  # noqa: BLE001
                counts["error"] += 1
                rows.append({"session_id": sid, "status": "error", "reason": repr(exc)})

        # flush the judge ledger so an interrupted run resumes cleanly
        judge_path.write_text(
            json.dumps(judge_results, ensure_ascii=False), encoding="utf-8"
        )
        done = min(offset + chunk, len(files))
        rate = done / max(1e-6, time.monotonic() - started)
        print(f"[ingest] {done}/{len(files)} ({rate:.1f}/s) {counts}", flush=True)

    report = {"counts": counts, "rows": rows}
    (run_dir / "ingest_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return report


async def _nowait(value):
    return value


# ------------------------------------------------------------------------- #
# Phase 2: evolve cycles                                                    #
# ------------------------------------------------------------------------- #

async def run_evolve(cfg, run_dir: Path, max_cycles: int) -> list[dict]:
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    server_cfg = EvolveServerConfig.from_teamEvolver_config(cfg)
    # from_teamEvolver_config does not read EVOLVE_HISTORY_LOG (only from_env
    # does), so pin the history/processed logs inside the run dir explicitly.
    server_cfg.history_path = str(run_dir / "evolve_history.jsonl")
    server_cfg.processed_log_path = str(run_dir / "evolve_processed.json")
    server = EvolveServer(server_cfg)

    summaries: list[dict] = []
    for cycle in range(1, max_cycles + 1):
        print(f"[evolve] === cycle {cycle} ===", flush=True)
        result = await server.run_once()
        summaries.append(result)
        print(
            f"[evolve] cycle {cycle}: sessions={result.get('sessions')} "
            f"groups={result.get('skill_groups')} actions={result.get('actions')} "
            f"uploaded={result.get('uploaded_skills')} "
            f"candidates={result.get('candidates_queued')} "
            f"elapsed={result.get('elapsed_seconds')}s "
            f"error={result.get('had_processing_error')}",
            flush=True,
        )
        if not result.get("sessions"):
            break
    return summaries


# ------------------------------------------------------------------------- #
# Phase 2b: backfill requeue for existing runs                              #
# ------------------------------------------------------------------------- #

def requeue_defective_sessions(cfg, threshold: float | None = None) -> dict:
    """Re-queue skipped sessions whose judge result shows defect evidence.

    Backfill for runs ingested before the ingest-time requeue branch existed.
    Only touches sessions whose index status is "skipped" — those were never
    queued and never consumed by an evolution cycle — so re-running is
    idempotent and already-evolved sessions are never re-processed.
    """
    from teamEvolver.evolve.stages.judge import judge_has_defect_evidence
    from teamEvolver.session_store import SessionStore

    store = SessionStore.from_config(cfg)
    counts = {"scanned": 0, "defect_candidates": 0, "requeued": 0,
              "already_queued": 0, "missing_archive": 0, "error": 0}

    try:
        rows = store.load_index_rows()
    except Exception as exc:  # noqa: BLE001
        print(f"[requeue] cannot load session index: {exc}", flush=True)
        return counts

    candidates = []
    for row in rows:
        if str(row.get("status") or "") != "skipped":
            continue
        counts["scanned"] += 1
        judge = row.get("judge") if isinstance(row.get("judge"), dict) else {}
        if judge_has_defect_evidence(judge, threshold=threshold):
            candidates.append(str(row.get("session_id") or ""))
    counts["defect_candidates"] = len(candidates)
    candidates = [sid for sid in candidates if sid]
    if not candidates:
        return counts

    statuses = store.conversation_statuses(candidates)
    for sid in candidates:
        if statuses.get(sid) == "queued":
            counts["already_queued"] += 1
            continue
        try:
            prior = store.load_archived(sid)
        except Exception:  # noqa: BLE001
            prior = None
        if not prior:
            counts["missing_archive"] += 1
            continue
        prior.pop("status", None)
        prior["requeue_reason"] = "judge_defect_evidence"
        store.save_queued(prior)
        counts["requeued"] += 1
    return counts


# ------------------------------------------------------------------------- #
# Phase 3: CSV export                                                       #
# ------------------------------------------------------------------------- #

def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _judge_case_type(overall) -> str:
    """Classify a judged session as good/mixed/bad.

    The judge prompt pins 0.5 as "好坏参半" (half good, half bad), so 0.5 is
    the natural mixed pivot; >= 0.7 is a solidly good case, < 0.5 a bad one.
    """
    try:
        score = float(overall)
    except (TypeError, ValueError):
        return ""
    if score >= 0.7:
        return "good_case"
    if score >= 0.5:
        return "mixed_case"
    return "bad_case"


def _task_completed(task_completion) -> str:
    """Derive task completion status from the judge's task_completion score.

    Same judge-prompt semantics as _judge_case_type: 0.5 marks
    "好坏参半 / 部分成功", so >= 0.7 counts as completed.
    """
    try:
        score = float(task_completion)
    except (TypeError, ValueError):
        return ""
    if score >= 0.7:
        return "yes"
    if score >= 0.5:
        return "partial"
    return "no"


def _fmt_evidence(ev: dict) -> str:
    """Compact one-line summary of a skill group's evidence classification."""
    ec = ev.get("evidence_classification")
    if not isinstance(ec, dict) or not ec:
        return ""
    parts: list[str] = []
    for bucket, label in (
        ("team_skill", "技能缺陷"),
        ("agent_runtime", "agent运行时"),
        ("user_memory", "用户记忆"),
        ("task_requirement", "任务需求"),
        ("insufficient_evidence", "证据不足"),
    ):
        items = ec.get(bucket) or []
        if not items:
            continue
        # LLM sometimes returns plain strings instead of {claim, ...} dicts
        first = items[0]
        claim = (
            str(first.get("claim") or "")
            if isinstance(first, dict)
            else str(first)
        )
        parts.append(f"{label}x{len(items)}: {claim[:80]}")
    return " | ".join(parts)


def _fmt_edit_summary(ev: dict) -> str:
    edit = ev.get("edit_summary")
    if not isinstance(edit, dict):
        return ""
    changed = "; ".join(str(c) for c in (edit.get("changed_sections") or [])[:6])
    notes = str(edit.get("notes") or "")[:200]
    out = f"changed: {changed}" if changed else ""
    if notes:
        out = f"{out} | notes: {notes}" if out else f"notes: {notes}"
    return out


def _fmt_file_changes(ev: dict) -> str:
    changes = ev.get("file_changes") or []
    if not isinstance(changes, list):
        return ""
    parts = []
    for c in changes[:8]:
        if isinstance(c, dict):
            parts.append(
                f"{c.get('path', '?')}({c.get('operation', '?')}): "
                f"{str(c.get('reason') or '')[:100]}"
            )
    return " | ".join(parts)


def export_csvs(run_dir: Path, store_root: Path) -> tuple[Path, Path]:
    history_path = run_dir / "evolve_history.jsonl"
    cycles = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            rec = _load_json_line(line)
            if rec:
                cycles.append(rec)

    # session -> judge scores. The ingest-phase judge ledger covers every
    # non-empty session (judging is decoupled from the evolution trigger);
    # history details (from evolve cycles) fill any gaps, e.g. sessions whose
    # ingest-phase judge call failed and were re-judged during evolution.
    judge_by_sid: dict[str, dict] = {}
    # session -> list of skill-group records it fed (a session referencing
    # skills A and B lands in both groups)
    groups_by_sid: dict[str, list[dict]] = {}
    for rec in cycles:
        for detail in rec.get("session_judge_details") or []:
            sid = str(detail.get("session_id") or "")
            if sid:
                judge_by_sid[sid] = detail
        for ev in rec.get("evolutions") or []:
            for sid in ev.get("session_ids") or []:
                sid = str(sid or "")
                if not sid:
                    continue
                groups_by_sid.setdefault(sid, []).append(ev)

    ledger = _load_json(run_dir / "judge_results.json")
    if isinstance(ledger, dict):
        for sid, scores in ledger.items():
            if isinstance(scores, dict) and scores.get("overall_score") is not None:
                judge_by_sid[str(sid)] = scores

    archive_dir = store_root / "session_filter_audit"
    payload_dir = store_root / "session_archive"
    ledger_dir = store_root / "session_ledger"

    session_csv = run_dir / "session_analysis.csv"
    columns = [
        "session_id", "user_alias", "timestamp", "title",
        "num_turns", "tool_call_count", "api_call_count", "total_tokens",
        "used_skills", "value_decision", "confidence", "reason",
        "ingest_status", "consumed",
        "task_completed", "judge_case_type", "judge_overall",
        "judge_task_completion",
        "judge_response_quality", "judge_efficiency", "judge_tool_usage",
        "judge_rationale",
        "skill_groups", "skill_group", "group_action", "group_rationale",
        "candidate_job_id", "candidate_revision",
        "evidence_summary", "edit_summary", "file_changes",
    ]
    n_rows = 0
    full_rows: list[dict] = []
    if archive_dir.exists():
        for path in sorted(archive_dir.glob("*.json")):
            audit = _load_json(path)
            if not isinstance(audit, dict):
                continue
            sid = str(audit.get("session_id") or "")
            judge = (
                audit.get("value_judge")
                if isinstance(audit.get("value_judge"), dict)
                else {}
            )
            # The evolve cycle re-archives consumed sessions in queue
            # schema (dropping value_judge); the archive payload still
            # carries metrics, so use it to enrich the audit row.
            payload = _load_json(payload_dir / path.name) or {}
            metrics = (
                payload.get("metrics")
                if isinstance(payload.get("metrics"), dict)
                else {}
            )
            ledger = _load_json(ledger_dir / path.name) or {}
            jd = judge_by_sid.get(sid, {})
            entries = groups_by_sid.get(sid) or []
            primary = entries[0] if entries else {}
            for ev in entries:
                if ev.get("validation_job_id"):
                    primary = ev
                    break
            group_list = "; ".join(
                f"{ev.get('skill_name', '?')}({ev.get('action', '?')})"
                for ev in entries
            )
            full_rows.append({
                "session_id": sid,
                "user_alias": audit.get("user_alias", ""),
                "timestamp": audit.get("timestamp", ""),
                "title": str(audit.get("title") or "")[:120],
                "num_turns": audit.get("num_turns", ""),
                "tool_call_count": audit.get("tool_call_count", 0),
                "api_call_count": metrics.get("api_call_count", ""),
                "total_tokens": audit.get("total_tokens", 0),
                "used_skills": ";".join(audit.get("used_skills") or []),
                "value_decision": judge.get("decision", ""),
                "confidence": judge.get("confidence", ""),
                "reason": str(judge.get("reason") or "")[:300],
                "ingest_status": audit.get("status", ""),
                "consumed": "yes" if str(ledger.get("status") or "") == "consumed" else "no",
                "task_completed": _task_completed(jd.get("task_completion")),
                "judge_case_type": _judge_case_type(jd.get("overall_score")),
                "judge_overall": jd.get("overall_score", ""),
                "judge_task_completion": jd.get("task_completion", ""),
                "judge_response_quality": jd.get("response_quality", ""),
                "judge_efficiency": jd.get("efficiency", ""),
                "judge_tool_usage": jd.get("tool_usage", ""),
                "judge_rationale": str(jd.get("rationale") or "")[:2000],
                "skill_groups": group_list,
                "skill_group": primary.get("skill_name", ""),
                "group_action": primary.get("action", ""),
                "group_rationale": str(primary.get("rationale") or "")[:1000],
                "candidate_job_id": primary.get("validation_job_id", ""),
                "candidate_revision": primary.get("candidate_revision", ""),
                "evidence_summary": _fmt_evidence(primary),
                "edit_summary": _fmt_edit_summary(primary),
                "file_changes": _fmt_file_changes(primary),
            })
            n_rows += 1

    with open(session_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(full_rows)

    # Sessions that never reached the archive (empty / duplicate / error)
    # still belong in the report so the CSV covers the full input set.
    report = _load_json(run_dir / "ingest_report.json") or {}
    seen = set()
    if archive_dir.exists():
        seen = {p.stem for p in archive_dir.glob("*.json")}
    with open(session_csv, "a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        for row in report.get("rows") or []:
            sid = str(row.get("session_id") or "")
            if not sid or sid in seen:
                continue
            status = str(row.get("status") or "")
            decision = row.get("decision") or (
                "empty" if status == "empty" else ""
            )
            writer.writerow({
                "session_id": sid,
                "ingest_status": status,
                "value_decision": decision,
                "reason": str(row.get("reason") or "")[:300],
            })
            n_rows += 1

    # Full-pipeline sessions only: classified valuable (queued), judged,
    # grouped by skill, and consumed by an evolution cycle.
    pipeline_csv = run_dir / "pipeline_sessions.csv"
    pipeline_rows = [
        row
        for row in full_rows
        if row.get("ingest_status") == "queued"
    ]
    with open(pipeline_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(pipeline_rows)

    # skill-level evolution summary
    skill_csv = run_dir / "skill_evolution_summary.csv"
    skill_columns = [
        "cycle", "skill_name", "action", "proposed_action", "uploaded",
        "validation_job_id", "candidate_revision", "version",
        "session_count", "session_ids",
        "distinct_users", "rationale",
        "evidence_team_skill", "evidence_agent_runtime",
        "evidence_user_memory", "evidence_task_requirement",
        "evidence_insufficient", "evidence_claims",
        "edit_summary", "file_changes",
        "coalesced", "test_dataset_count",
    ]
    # session -> user mapping from cycle summaries for the distinct-user count
    senders: dict[str, str] = {}
    for rec in cycles:
        for sid, user in (rec.get("session_senders") or {}).items():
            senders[str(sid)] = str(user)
    with open(skill_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=skill_columns)
        writer.writeheader()
        for i, rec in enumerate(cycles, start=1):
            for ev in rec.get("evolutions") or []:
                ec = (
                    ev.get("evidence_classification")
                    if isinstance(ev.get("evidence_classification"), dict)
                    else {}
                )
                session_ids = [str(s) for s in (ev.get("session_ids") or [])]
                writer.writerow({
                    "cycle": i,
                    "skill_name": ev.get("skill_name", ""),
                    "action": ev.get("action", ""),
                    "proposed_action": ev.get("proposed_action", ""),
                    "uploaded": bool(ev.get("uploaded")),
                    "validation_job_id": ev.get("validation_job_id", ""),
                    "candidate_revision": ev.get("candidate_revision", ""),
                    "version": ev.get("version", ""),
                    "session_count": len(session_ids),
                    "session_ids": ";".join(session_ids[:10]),
                    "distinct_users": len({senders.get(s, "?") for s in session_ids}),
                    "rationale": str(ev.get("rationale") or "")[:2000],
                    "evidence_team_skill": len(ec.get("team_skill") or []),
                    "evidence_agent_runtime": len(ec.get("agent_runtime") or []),
                    "evidence_user_memory": len(ec.get("user_memory") or []),
                    "evidence_task_requirement": len(ec.get("task_requirement") or []),
                    "evidence_insufficient": len(ec.get("insufficient_evidence") or []),
                    "evidence_claims": _fmt_evidence(ev),
                    "edit_summary": _fmt_edit_summary(ev),
                    "file_changes": _fmt_file_changes(ev),
                    "coalesced": bool(ev.get("coalesced")),
                    "test_dataset_count": ev.get("test_dataset_count", ""),
                })
    return session_csv, pipeline_csv, skill_csv


def _load_json_line(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------- #
# main                                                                       #
# ------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", type=Path, default=SESSIONS_DIR_DEFAULT)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--classify-concurrency", type=int, default=8)
    parser.add_argument("--max-cycles", type=int, default=30)
    parser.add_argument("--drain-max-per-cycle", type=int, default=40)
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument(
        "--requeue-defective",
        action="store_true",
        help="backfill: re-queue sessions archived as skipped whose judge "
        "score shows defect evidence (overall < threshold or explicit defect "
        "flag), then re-export the CSVs. Only touches never-consumed "
        "(index status 'skipped') sessions, so it is idempotent.",
    )
    parser.add_argument(
        "--requeue-threshold",
        type=float,
        default=None,
        help="overall-score threshold for --requeue-defective "
        "(default: EVOLVE_REQUEUE_DEFECT_THRESHOLD env or 0.5)",
    )
    parser.add_argument(
        "--skip-skill-seed",
        action="store_true",
        help="do not seed the local store with the current team skills from "
        "OpenViking before evolving (seeding is what gives the planner real "
        "SKILL.md baselines)",
    )
    args = parser.parse_args()

    run_dir = args.run_dir or RUNS_ROOT / f"evolution_local_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    store_root = run_dir / "local_store"

    if args.export_only:
        session_csv, pipeline_csv, skill_csv = export_csvs(run_dir, store_root)
        print(f"exported: {session_csv}\n           {pipeline_csv}\n           {skill_csv}")
        return 0

    os.environ.update({
        "EVOLVE_STORAGE_BACKEND": "local",
        "EVOLVE_STORAGE_LOCAL_ROOT": str(store_root),
        "EVOLVE_HISTORY_LOG": str(run_dir / "evolve_history.jsonl"),
        "EVOLVE_PROCESSED_LOG": str(run_dir / "evolve_processed.json"),
        "EVOLVE_DRAIN_MAX_PER_CYCLE": str(args.drain_max_per_cycle),
    })

    from teamEvolver.config_store import ConfigStore

    cfg = ConfigStore().to_config()
    # Route the session queue + skill store to the same local root as the
    # evolve engine so every component shares one LocalObjectStore.
    cfg.sharing_session_backend = "local"
    cfg.sharing_local_root = str(store_root)

    if args.requeue_defective:
        counts = requeue_defective_sessions(cfg, threshold=args.requeue_threshold)
        print(f"[requeue] {counts}", flush=True)
        session_csv, pipeline_csv, skill_csv = export_csvs(run_dir, store_root)
        print(f"[export] {session_csv}")
        print(f"[export] {pipeline_csv}")
        print(f"[export] {skill_csv}")
        return 0

    if not args.skip_skill_seed:
        seed_current_skills(store_root)

    async def _run() -> None:
        print(f"[run] store={store_root}", flush=True)
        report = await run_ingest(cfg, args.sessions_dir, run_dir, args.classify_concurrency)
        print(f"[ingest] final: {report['counts']}", flush=True)
        await run_evolve(cfg, run_dir, args.max_cycles)

    asyncio.run(_run())

    session_csv, pipeline_csv, skill_csv = export_csvs(run_dir, store_root)
    print(f"[export] {session_csv}")
    print(f"[export] {pipeline_csv}")
    print(f"[export] {skill_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
