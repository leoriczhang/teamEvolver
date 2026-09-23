import json
import re
from dataclasses import replace

import pytest
from test_successful_experience_sync import Store, source, worker

from team_skills.library.experience_import import staged_documents
from team_skills.library.experience_sync import SyncError, canonical


class ImportStore(Store):
    def __init__(self, tenant="a"):
        super().__init__(tenant)
        self.index = []

    def session_experience_page(self, *, after_index="", after_session="", until, limit=100):
        return sorted([r for r in self.index if (r["index_key"], r["session_id"]) > (after_index, after_session)],
                      key=lambda r: (r["index_key"], r["session_id"]))[:limit]

    def experience_import_sources(self, *, phase, pattern, until, limit, after_time, after_key,
                                  after_index, after_session):
        if phase == "objects":
            return [{"phase": phase, "key": k, "size": len(self.get_object(k).read()), "revision": "test",
                     "updated_at": at} for k, at in sorted(self.times.items(), key=lambda kv: (kv[1], kv[0]))
                    if re.search(pattern, k) and (at, k) > (after_time, after_key) and at <= until][:limit]
        return [{"phase": phase, "key": r["index_key"], "session_id": r["session_id"], "revision": "test",
                 "size": len(canonical(r["meta"])), "updated_at": self.stamp()}
                for r in self.session_experience_page(after_index=after_index, after_session=after_session,
                                                       until=until, limit=limit)]

    def experience_import_unchanged(self, source):
        return True

    def experience_import_page(self, source, *, offset, max_source_bytes, limit):
        from team_skills.library.experience_import import history_rows

        if source["size"] > max_source_bytes:
            return {"error": "IMPORT_SOURCE_TOO_LARGE"}
        try:
            if source["phase"] == "objects":
                payload = json.loads(self.get_object(source["key"]).read())
            else:
                payload = next(row["meta"] for row in self.index if row["session_id"] == source["session_id"]
                               and row["index_key"] == source["key"])
            evidence = "/skill_evidence/" in "/" + source["key"]
            if evidence:
                records = [{"skill_name": payload["skill_name"], **row} for row in payload["evidence"]]
            else:
                records = []
                for row in history_rows(source["key"], payload, index=source["phase"] == "index"):
                    for entry in row.get("experiences") or []:
                        records.append({"session_id": row.get("session_id"), "timestamp": row.get("timestamp"),
                                        "ingested_at": row.get("ingested_at"), **entry})
            return {"total": len(records), "records": [
                {"ordinal": index + 1, "record": row, "record_bytes": len(canonical(row)), "error": None}
                for index, row in enumerate(records) if offset <= index < offset + limit]}
        except (SyncError, ValueError, TypeError, KeyError) as exc:
            return {"error": exc.code if isinstance(exc, SyncError) else "INVALID_IMPORT_SOURCE"}


def lesson(description="先验证再发布", **changes):
    return {"skill_name": "review", "kind": "exemplary", "experience_key": "verify",
            "description": description, **changes}


def session(sid="s1", description="先验证再发布", **changes):
    return {"session_id": sid, "timestamp": "2026-09-01", "user_alias": "private-person",
            "experiences": [lesson(description)], **changes}


def indexed(store, value):
    store.index.append({"index_key": "session_index.json", "session_id": value["session_id"], "meta": value})


def finish(w):
    for _ in range(300):
        w.run_once()
        if w.status()["manual"]["state"] == "completed":
            return
    pytest.fail("import did not complete")


def test_import_all_sources_dedup_excludes_defects_and_never_exports_private_fields():
    store = ImportStore()
    w = worker(store, batch_size=2)
    store.put_object("experience_library/review.json", canonical(source()))
    store.put_object("experience_library/sessions/old.json", canonical(session(description="旧文本")))
    store.put_object("session_archive/s1.json", canonical({
        "session_id": "s1", "timestamp": "2026-09-01",
        "_judge_scores": {"skill_experiences": [lesson("归档文本")]},
        "messages": [{"content": "private-trajectory"}],
    }))
    store.put_object("skill_evidence/review.json", canonical({"skill_name": "review", "evidence": [
        {"session_id": "historical", "evolution_evidence": "exemplary", "evidence_reason": "历史成功实践"},
        {"session_id": "defect", "evolution_evidence": "defect", "evidence_reason": "禁止导出"},
    ]}))
    store.put_object("session_archive/.hidden.json", canonical({"session_id": "hidden"}))
    indexed(store, session(description="最终文本"))
    indexed(store, session("s2", experiences=[lesson(kind="defect")]))
    w.run_once()  # Regular sync continues to ignore Session sources.
    assert w.writer.writes == 1
    receipt = w.request_run("import_all")
    assert receipt["operation"] == "import_all" and w.writer.writes == 1
    assert w.request_run("import_all")["request_id"] == receipt["request_id"]
    with pytest.raises(SyncError, match="SYNC_OTHER_OPERATION_RUNNING"):
        w.request_run()
    finish(w)
    progress = w.status()["manual"]["import"]
    assert progress["phase"] == "complete"
    assert progress["processed_sources"] == 5  # 3 history objects, 2 index rows
    assert progress["prepared_documents"] == 2
    assert progress["rejected_sources"] == 0
    assert w.writer.writes == 3  # Legacy per-Skill doc + 2 imported lessons.
    exported = b"\n".join(w.writer.content.values()).decode()
    for forbidden in ["private-person", "private-trajectory", "session_ids", "occurrence_count", "禁止导出"]:
        assert forbidden not in exported
    assert "最终文本" in exported and "历史成功实践" in exported
    assert "旧文本" not in exported and "归档文本" not in exported


def test_reimport_count_changes_do_not_write_and_text_changes_reuse_uri():
    store = ImportStore()
    w = worker(store)
    indexed(store, session())
    w.request_run("import_all")
    finish(w)
    original_uri = next(iter(w.writer.content))
    indexed(store, session("s2", timestamp="2026-09-02", occurrence_count=42, latest_score=.95))
    w.request_run("import_all")
    finish(w)
    assert w.writer.writes == 1
    store.index[-1]["meta"]["experiences"] = [lesson("修改后的正文")]
    w.request_run("import_all")
    finish(w)
    assert w.writer.writes == 2 and list(w.writer.content) == [original_uri]
    assert json.loads(w.writer.content[original_uri])["description"] == "修改后的正文"
    store.index.clear()
    w.request_run("import_all")
    finish(w)
    assert w.writer.writes == 2 and len(w.writer.content) == 1
    assert w.status()["manual"]["import"]["prepared_documents"] == 0


def test_import_restart_cursor_and_stage_durability(monkeypatch):
    store = ImportStore()
    w = worker(store, batch_size=1)
    for n in range(3):
        indexed(store, session(str(n), experiences=[lesson(experience_key=f"lesson-{n}")]))
    receipt = w.request_run("import_all")
    w.run_once()  # No objects; next page is Session index.
    save = store.batch_write
    failed = False

    def interrupt_meta(objects, *, preconditions):
        nonlocal failed
        if any("/import-pages/" in key for key in objects) and not failed:
            failed = True
            raise RuntimeError("crash before atomic checkpoint")
        save(objects, preconditions=preconditions)

    monkeypatch.setattr(store, "batch_write", interrupt_meta)
    with pytest.raises(RuntimeError, match="crash"):
        w.run_once()
    resumed = worker(store, w.writer, batch_size=1)
    assert resumed.request_run("import_all")["request_id"] == receipt["request_id"]
    finish(resumed)
    progress = resumed.status()["manual"]["import"]
    assert progress["processed_sources"] == 3 and progress["prepared_documents"] == 3
    assert resumed.writer.writes == 3


def test_invalid_sources_are_counted_without_body_leaks_or_blocking_valid_rows():
    store = ImportStore()
    w = worker(store)
    store.put_object("experience_library/sessions/bad.json", b"private-invalid-json")
    store.put_object("session_archive/bad-score.json", canonical({"_judge_scores": ["invalid"]}))
    indexed(store, {"session_id": "missing-field"})
    indexed(store, session())
    store.index.append({"index_key": "session_index.json", "session_id": "oversized", "meta": None})
    w.request_run("import_all")
    finish(w)
    result = w.status()
    assert result["manual"]["import"]["rejected_sources"] == 4
    assert result["manual"]["import"]["prepared_documents"] == 1
    assert w.writer.writes == 1
    assert "private-invalid-json" not in json.dumps(result)


def test_import_failure_keeps_upload_retry_and_other_tenants_independent():
    store = ImportStore("a")
    w = worker(store)
    w.writer.mode = "index_failed"
    indexed(store, session())
    w.request_run("import_all")
    finish(w)
    assert w.status()["counts"]["retry"] == 1
    w.request_run("import_all")
    finish(w)
    assert w.writer.writes == 1
    other = worker(ImportStore("b"), w.writer)
    assert other.status()["manual"] is None
    w.writer.mode = "ok"
    store.now += 60
    w.run_once()
    assert w.status()["counts"]["synced"] == 1
    w.settings = replace(w.settings, enabled=False)
    with pytest.raises(SyncError, match="SYNC_DISABLED"):
        w.request_run("import_all")


def test_import_resume_honors_stop_before_advancing_cursor():
    w = worker(ImportStore())
    indexed(w.store, session())
    w.request_run("import_all")
    w.stop.set()
    w.run_once()
    assert w.status()["manual"]["state"] == "queued"
    assert w.writer.writes == 0
    w.stop.clear()
    finish(w)
    assert w.writer.writes == 1


def test_duplicate_quote_ties_and_identity_are_stable():
    a = staged_documents("session_index.json", session(), index=True)
    b = staged_documents("session_index.json", session(user_alias="other", occurrence_count=99), index=True)
    assert a == b
    assert a[0][1]["document"]["kind"] == "exemplary"


def test_upgrade_retains_old_cursor_and_historical_gap_until_new_import():
    store = ImportStore()
    w = worker(store)
    for name in ("a", "b"):
        store.put_object(f"session_archive/{name}.json", canonical({
            "session_id": name, "judge": {"skill_experiences": [lesson(experience_key=name)]}}))
    w.request_run("import_all")
    meta = w.load(w.meta_key)
    meta["manual"]["import"].update(processed_sources=1, rejected_sources=1, last_error="IMPORT_SOURCE_TOO_LARGE",
                                    after_key="session_archive/a.json", after_time=store.stamp())
    store.put_object(w.meta_key, canonical(meta))
    finish(w)
    report = w.status()["manual"]["import"]
    assert report["processed_sources"] == 2 and report["rejected_sources"] == 1
    assert w.writer.writes == 1
    w.request_run("import_all")
    finish(w)
    assert w.status()["manual"]["import"]["rejected_sources"] == 0 and w.writer.writes == 2


def test_projection_query_timeout_is_safe_and_honors_smaller_budget():
    from types import SimpleNamespace

    from teamEvolver.storage.experience_import import project_page

    seen = []

    def run(coro, *, timeout):
        coro.close()
        seen.append(timeout)
        raise TimeoutError("private DSN should not leak")

    store = SimpleNamespace(_schema="teamevolver", tenant_id="a", _command_timeout=.5, _op_timeout=1,
                            _runtime=SimpleNamespace(run=run))
    result = project_page(store, {"phase": "objects", "key": "session_archive/a.json", "size": 200,
                                  "revision": "1", "updated_at": "2026-09-22"},
                          offset=0, max_source_bytes=1048576)
    assert result == {"error": "IMPORT_QUERY_TIMEOUT"} and seen == [.5]
