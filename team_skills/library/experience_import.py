"""Explicit, resumable import of Session-backed successful experiences.

Only administrator import requests enter this path. It neither changes Judge
writes nor loads the experience-page cache (which has a 10,000 Session limit).
"""

from __future__ import annotations

import json
import logging
import time

from team_skills.evolution.experience_library import _digest, session_experiences
from team_skills.library.experience_sync import LOCK, SyncError, canonical, digest, documents
from teamEvolver.logging_runtime import event

LOG = logging.getLogger(__name__)
HISTORY_PATTERN = (
    r"(^|/)(experience_library/sessions|skill_evidence|session_archive|sessions)/[^./][^/]*[.]json$"
)


def history_rows(key, payload, *, index=False):
    if not isinstance(payload, dict):
        raise SyncError("INVALID_IMPORT_SOURCE")
    if "/skill_evidence/" in "/" + key:
        evidence = payload.get("evidence") or []
        if not isinstance(evidence, list) or len(evidence) > 1000:
            raise SyncError("INVALID_IMPORT_SOURCE")
        for row in evidence:
            if not isinstance(row, dict):
                raise SyncError("INVALID_IMPORT_SOURCE")
            reason = str(row.get("evidence_reason") or "").strip()
            kind = str(row.get("evolution_evidence") or "").strip().lower()
            if kind == "exemplary" and reason:
                yield {**row, "experiences": [{
                    "skill_name": str(payload.get("skill_name") or "").strip(), "kind": kind,
                    "experience_key": f"legacy_{_digest(reason)}", "description": reason,
                }]}
    elif index or "/experience_library/sessions/" in "/" + key:
        if "experiences" not in payload:
            # Raw archives are scanned separately; do not claim missing index
            # fields are empty successful results.
            raise SyncError("IMPORT_INDEX_EXPERIENCES_MISSING")
        yield payload
    else:
        for name in ("_judge_scores", "judge"):
            scores = payload.get(name)
            if scores is not None and (not isinstance(scores, dict) or (
                "skill_experiences" in scores and not isinstance(scores["skill_experiences"], list)
            )):
                raise SyncError("INVALID_IMPORT_SOURCE")
        yield {**payload, "experiences": session_experiences(payload)}


def staged_documents(key, payload, *, index=False, projected=False):
    """Validate a bounded source before persisting any of its proposals."""
    result = []
    for row in ([payload] if projected else history_rows(key, payload, index=index)):
        sid = str(row.get("session_id") or "").strip()
        if not sid:
            raise SyncError("IMPORT_SESSION_ID_MISSING")
        lessons = row.get("experiences") or []
        if not isinstance(lessons, list) or len(lessons) > 1000:
            raise SyncError("INVALID_IMPORT_SOURCE")
        for lesson in lessons:
            if not isinstance(lesson, dict) or lesson.get("kind") != "exemplary":
                continue
            skill = lesson.get("skill_name")
            experience_key = lesson.get("experience_key")
            if not isinstance(skill, str) or not skill.strip() or not isinstance(experience_key, str):
                raise SyncError("INVALID_IMPORT_SOURCE")
            skill = skill.strip()
            identity = _digest(skill, "exemplary", experience_key)
            source_key = f"experience_library/session-derived-{digest(skill.encode())}.json"
            [(path, doc)] = documents(source_key, canonical({
                "skill_name": skill, "experiences": [{**lesson, "id": identity}],
            }))
            # Latest observed lesson wins; stable ties prefer the Session index.
            # Statistics, users and full trajectories never enter the OV document.
            rank = [str(row.get("timestamp") or row.get("ingested_at") or ""), sid,
                    "1" if index else "0", key, digest(doc["description"].encode())]
            result.append((identity, {"path": path, "document": doc, "rank": rank}))
    return result


def _commit(worker, meta, values=None):
    worker.store.check_background_lock(LOCK)
    worker.store.batch_write({**{k: canonical(v) for k, v in (values or {}).items()},
                              worker.meta_key: canonical(meta)}, preconditions={})


def _gap(worker, meta, pending, code, *, ordinal=0, record_bytes=None, limit_bytes=None):
    progress = meta["manual"]["import"]
    source = pending["source"]
    detail = {"source_key": source["key"], "session_id": source.get("session_id"), "code": code,
              "source_bytes": source["size"], "revision": source["revision"], "stage": pending["stage"],
              "ordinal": ordinal, "record_bytes": record_bytes, "limit_bytes": limit_bytes}
    if not pending.get("has_errors"):
        progress["rejected_sources"] += 1
        pending["has_errors"] = True
    if ordinal:
        progress["rejected_records"] = progress.get("rejected_records", 0) + 1
    counts = progress.setdefault("error_counts", {})
    counts[code] = counts.get(code, 0) + 1
    samples = progress.setdefault("errors_sample", [])
    if len(samples) < 20:
        samples.append(detail)
    progress["last_error"] = code
    error_key = worker.base + "import-errors/" + meta["manual"]["request_id"] + "/" + digest(canonical(
        [source, ordinal, code])) + ".json"
    event(LOG, "experience_sync.import_source_rejected", logging.WARNING,
          tenant=worker.store.tenant_id, sync_request_id=meta["manual"]["request_id"], **detail)
    return error_key, detail


def _projected_proposals(source, record):
    key = source["key"]
    if "/skill_evidence/" in "/" + key:
        return staged_documents(key, {"skill_name": record.get("skill_name"), "evidence": [record]})
    lesson = {field: record.get(field) for field in ("skill_name", "kind", "experience_key", "description")}
    if source["phase"] == "objects" and "/experience_library/sessions/" not in "/" + key:
        # Match Judge identity normalization, but never truncate the lesson body.
        lesson = {"skill_name": str(lesson["skill_name"] or "").strip()[:200],
                  "kind": str(lesson["kind"] or "").strip().lower(),
                  "experience_key": str(lesson["experience_key"] or "").strip()[:160].lower(),
                  "description": str(lesson["description"] or "").strip()}
    row = {**record, "experiences": [lesson]}
    return staged_documents(key, row, index=source["phase"] == "index", projected=True)


def _finish_source(worker, meta):
    progress = meta["manual"]["import"]
    source = progress["source"]["source"]
    progress["processed_sources"] += 1
    if source["phase"] == "objects":
        progress.update(after_time=source["updated_at"], after_key=source["key"])
    else:
        progress.update(after_index=source["key"], after_session=source["session_id"])
    progress.pop("source")
    worker.save(worker.meta_key, meta)


def _advance_source(worker, meta):
    """One bounded page; unconfirmed source pages cannot affect global groups."""
    manual, progress = meta["manual"], meta["manual"]["import"]
    pending = progress["source"]
    source, prefix = pending["source"], pending["prefix"]
    stage = pending["stage"]
    if stage == "parse":
        maximum = worker.settings.import_max_source_mb * 1024 * 1024
        result = worker.store.experience_import_page(source, offset=pending["offset"],
                                                     max_source_bytes=maximum, limit=worker.settings.batch_size)
        writes = {}
        if result.get("error"):
            key, detail = _gap(worker, meta, pending, result["error"], limit_bytes=(
                maximum if result["error"] == "IMPORT_SOURCE_TOO_LARGE" else None))
            writes[key] = detail
            pending["stage"] = "cleanup"
        else:
            proposals = []
            for row in result["records"]:
                if row.get("skip"):
                    pending["offset"] = row["ordinal"]
                    continue
                code = row.get("error")
                if not code:
                    try:
                        proposals.extend(_projected_proposals(source, row["record"]))
                    except (SyncError, ValueError, TypeError, KeyError):
                        code = "INVALID_IMPORT_RECORD"
                if code:
                    key, detail = _gap(worker, meta, pending, code, ordinal=row["ordinal"],
                                       record_bytes=row.get("record_bytes"), limit_bytes=256 * 1024)
                    writes[key] = detail
                pending["offset"] = row["ordinal"]
            if proposals:
                writes[prefix + f"{pending['offset']:012}.json"] = proposals
                pending["eligible"] += len(proposals)
            if pending["offset"] >= result["total"]:
                pending["stage"] = "confirm"
        _commit(worker, meta, writes)
    elif stage == "confirm":
        code = None
        try:
            if not worker.store.experience_import_unchanged(source):
                code = "IMPORT_SOURCE_CHANGED"
        except TimeoutError:
            code = "IMPORT_QUERY_TIMEOUT"
        writes = {}
        if code:
            key, detail = _gap(worker, meta, pending, code)
            writes[key] = detail
            pending["stage"] = "cleanup"
        else:
            progress["eligible_records"] += pending["eligible"]
            pending["stage"] = "merge"
        _commit(worker, meta, writes)
    elif stage == "merge":
        pages = worker.store.object_page(prefix=prefix, after_key=pending.get("merge_cursor", ""), limit=1)
        if not pages:
            pending["stage"] = "cleanup"
        else:
            page = pages[0]
            for identity, value in json.loads(page["content"]):
                stage_key = worker.base + "import-groups/" + identity + ".json"
                old = worker.load(stage_key)
                if old.get("request_id") != manual["request_id"] or old["rank"] < value["rank"]:
                    worker.save(stage_key, {**value, "request_id": manual["request_id"]})
            pending["merge_cursor"] = page["key"]
        worker.save(worker.meta_key, meta)
    else:  # Clean only this source's private temporary pages, never delivery records.
        pages = worker.store.object_page(prefix=prefix, limit=1)
        if pages:
            worker.store.check_background_lock(LOCK)
            worker.store.delete_object(pages[0]["key"])
        else:
            _finish_source(worker, meta)


def advance_import(worker, meta):
    manual = meta["manual"]
    progress = manual["import"]
    phase = progress["phase"]
    if phase == "complete":
        return False
    deadline = time.monotonic() + 20
    if phase == "prepare":
        prefix = worker.base + "import-groups/"
        rows = worker.store.object_page(prefix=prefix, after_key=progress.get("group_cursor", ""),
                                        limit=worker.settings.batch_size)
        for row in rows:
            if worker.stop.is_set() or time.monotonic() >= deadline:
                return True
            value = json.loads(row["content"])
            if value["request_id"] == manual["request_id"]:
                worker.enqueue(value["path"], value["document"], verify=True)
                progress["prepared_documents"] += 1
            progress["group_cursor"] = row["key"]
            worker.save(worker.meta_key, meta)
        if len(rows) < worker.settings.batch_size:
            progress["phase"] = "complete"
            worker.save(worker.meta_key, meta)
            event(LOG, "experience_sync.import_prepared", tenant=worker.store.tenant_id,
                  sync_request_id=manual["request_id"], documents=progress["prepared_documents"],
                  rejected_sources=progress["rejected_sources"])
        return True

    # Metadata pages preserve the legacy outer cursors. Existing jobs are not
    # reset; new per-source checkpoints start at the next unprocessed source.
    remaining = worker.settings.batch_size
    while remaining and not worker.stop.is_set() and time.monotonic() < deadline:
        if progress.get("source"):
            _advance_source(worker, meta)
            remaining -= 1
            continue
        rows = worker.store.experience_import_sources(
            phase=phase, pattern=HISTORY_PATTERN, until=progress["until"], limit=1,
            after_time=progress.get("after_time", "-infinity"), after_key=progress.get("after_key", ""),
            after_index=progress.get("after_index", ""), after_session=progress.get("after_session", ""),
        )
        if not rows:
            progress["phase"] = "index" if phase == "objects" else "prepare"
            worker.save(worker.meta_key, meta)
            break
        source = rows[0]
        prefix = worker.base + "import-pages/" + manual["request_id"] + "/" + digest(canonical(source)) + "/"
        progress["source"] = {"source": source, "prefix": prefix, "stage": "parse", "offset": 0, "eligible": 0}
        worker.save(worker.meta_key, meta)
    return True
