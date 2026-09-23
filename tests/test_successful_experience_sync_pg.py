"""Opt-in tests against an isolated, non-superuser PostgreSQL database.

Set TE_EXPERIENCE_TEST_PG_DSN to a disposable database. Each test owns a unique
schema, created and removed with that role; no deployment configuration is read.
"""

import asyncio
import os
import time
import uuid
from dataclasses import replace

import pytest
from test_successful_experience_sync import source, worker

from team_skills.library.experience_sync import LOCK, SOURCE_PATTERN, ExperienceSync, canonical, digest
from teamEvolver.storage.pg_pool import PgRuntime
from teamEvolver.storage.pg_store import PgObjectStore


@pytest.fixture
def pg_stores():
    dsn = os.environ.get("TE_EXPERIENCE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("isolated PG DSN not configured")
    import asyncpg

    schema = "te_exp_" + uuid.uuid4().hex[:12]
    runtimes = []

    def create(tenant="a"):
        # Separate runtimes simulate separate replicas/connections.
        runtime = PgRuntime(dsn=dsn, schema=schema, pool_min=1, pool_max=4, ssl="disable")
        runtimes.append(runtime)
        return PgObjectStore(dsn=dsn, schema=schema, tenant_id=tenant, runtime=runtime)

    yield create
    for runtime in runtimes:
        runtime.close()

    async def cleanup():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()

    asyncio.run(cleanup())


def test_pg_pagination_rls_and_nonblocking_replica_mutex(pg_stores):
    a, replica, b = pg_stores(), pg_stores(), pg_stores("b")
    for n in range(5):
        a.put_object(f"experience_library/{n}.json", canonical(source()))
    b.put_object("experience_library/private.json", canonical(source("tenant b")))

    async def equal_timestamps():
        async with a._runtime.tenant_conn("a") as conn:
            await conn.execute(f"UPDATE {a._schema}.objects SET updated_at='2026-01-01' WHERE tenant_id=$1", "a")
            # RLS must also hold without an explicit tenant predicate.
            rows = await conn.fetch(f"SELECT tenant_id FROM {a._schema}.objects")
            assert {row["tenant_id"] for row in rows} == {"a"}

    a._runtime.run(equal_timestamps())
    until = a.database_time()
    after_time, after_key, keys = "-infinity", "", []
    while True:
        rows = a.changed_objects_page(pattern=SOURCE_PATTERN, after_time=after_time,
                                      after_key=after_key, until=until, limit=2)
        if not rows:
            break
        keys.extend(row["key"] for row in rows)
        after_time, after_key = rows[-1]["updated_at"], rows[-1]["key"]
    assert len(keys) == 5 and len(set(keys)) == 5
    assert a.try_background_lock(LOCK)
    try:
        assert not replica.try_background_lock(LOCK)
        assert b.try_background_lock(LOCK)
        b.release_background_lock(LOCK)
        assert replica._runtime.try_advisory_lock("a")  # independent evolution lock
        replica._runtime.release_advisory_lock("a")
        a.check_background_lock(LOCK)
    finally:
        a.release_background_lock(LOCK)
    assert replica.try_background_lock(LOCK)
    replica.release_background_lock(LOCK)


def test_pg_backfill_and_cas_restart(pg_stores):
    store = pg_stores()
    template = worker()
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    store.put_object("experience_library/a.json", canonical(source()))
    w.run_once()
    assert w.status()["counts"]["synced"] == 1
    replica = pg_stores()
    restored = ExperienceSync(replica, template.target, template.settings, template.writer)
    restored.run_once()
    assert template.writer.writes == 1
    store.put_object("experience_library/a.json", canonical(source(occurrence_count=123)))
    restored.run_once()
    assert template.writer.writes == 1
    row = store.object_page(prefix=w.items)[0]
    replica.put_object(row["key"], b'{"changed":true}')
    with pytest.raises(RuntimeError):
        store.batch_write({row["key"]: row["content"]}, preconditions={row["key"]: {
            "kind": "replace_if_hash", "base_hash": "sha256:" + digest(row["content"]),
        }})


def test_pg_large_objects_are_not_loaded(pg_stores):
    store = pg_stores()
    store.put_object("experience_library/large.json", b"x" * 1048577)
    rows = store.changed_objects_page(pattern=SOURCE_PATTERN, after_time="-infinity", after_key="",
                                      until=store.database_time(), limit=1)
    assert rows[0]["content"] is None and rows[0]["size"] == 1048577


def test_pg_lock_connection_loss_fails_closed(pg_stores):
    store, replica = pg_stores(), pg_stores()
    assert store.try_background_lock(LOCK)

    async def terminate():
        store._runtime._advisory_conns[f"{LOCK}:a"].terminate()

    store._runtime.run(terminate())
    with pytest.raises(RuntimeError, match="BACKGROUND_LOCK_LOST"):
        store.check_background_lock(LOCK)
    store.release_background_lock(LOCK)
    assert replica.try_background_lock(LOCK)
    replica.release_background_lock(LOCK)


def test_pg_commit_after_scan_repaired_by_full_scan(pg_stores):
    store = pg_stores()
    store.put_object("anchor", b"{}")  # ensure tenant and pool
    now = [time.time()]
    template = worker()
    w = ExperienceSync(store, template.target, replace(template.settings, full_scan_interval_seconds=60),
                       template.writer, clock=lambda: now[0])

    async def begin():
        pool = await store._runtime._ensure_pool()
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.tenant_id','a',true)")
        await conn.execute(
            f"INSERT INTO {store._schema}.objects(tenant_id,key,content,updated_at) "
            "VALUES('a','experience_library/late.json',$1,now()-interval '10 minutes')", canonical(source()))
        return conn, tx

    conn, tx = store._runtime.run(begin())
    try:
        w.run_once()
        assert not template.writer.content
    finally:
        async def commit():
            await tx.commit()
            await store._runtime._pool.release(conn)
        store._runtime.run(commit())
    w.run_once()
    assert not template.writer.content  # older than the overlap window
    now[0] += 61
    w.run_once()
    assert template.writer.writes == 1


def test_pg_manual_request_survives_replica_restart_and_tenant_isolation(pg_stores):
    store, replica, other = pg_stores(), pg_stores(), pg_stores("b")
    template = worker(batch_size=2)
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    restored = ExperienceSync(replica, template.target, template.settings, template.writer)
    other_worker = ExperienceSync(other, template.target, template.settings, template.writer)
    for n in range(5):
        store.put_object(f"experience_library/{n}.json", canonical(source()))
    receipt = w.request_run()
    assert restored.request_run()["request_id"] == receipt["request_id"]
    assert other_worker.status()["manual"] is None
    assert template.writer.writes == 0
    for _ in range(15):
        if not restored.run_once():
            break
    assert w.status()["manual"]["state"] == "completed"
    assert w.status()["counts"]["synced"] == 5
    assert template.writer.writes == 5


def test_pg_full_import_paginates_more_than_ui_limit_and_excludes_other_tenants(pg_stores):
    store, other = pg_stores(), pg_stores("b")

    async def seed():
        async with store._runtime.tenant_conn("a") as conn:
            await conn.execute(
                f"INSERT INTO {store._schema}.session_index (tenant_id,index_key,session_id,meta) "
                "SELECT 'a','session_index.json',lpad(n::text,6,'0'),"
                "jsonb_build_object('session_id',n::text,'experiences','[]'::jsonb) "
                "FROM generate_series(1,10005) AS n"
            )

    store._runtime.run(seed())
    other.write_session_records({}, "session_index.json", [{"session_id": "b-only", "experiences": []}])
    until = store.database_time()
    cursor, seen = ("", ""), []
    while True:
        rows = store.experience_import_sources(phase="index", pattern="", after_index=cursor[0],
                                               after_session=cursor[1], until=until, limit=100)
        if not rows:
            break
        assert len(rows) <= 100
        seen.extend(row["session_id"] for row in rows)
        cursor = rows[-1]["key"], rows[-1]["session_id"]
    assert len(seen) == 10005 and len(set(seen)) == 10005 and "b-only" not in seen
    assert len(other.session_experience_page(until=other.database_time())) == 1


def test_pg_import_session_history_resume_and_content_updates(pg_stores):
    from test_successful_experience_import import finish, session

    store, replica = pg_stores(), pg_stores()
    template = worker(batch_size=1)
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    row = session()
    store.write_session_records({}, "session_index.json", [row])
    w.request_run("import_all")
    w.run_once()
    restored = ExperienceSync(replica, template.target, template.settings, template.writer)
    finish(restored)
    assert w.status()["manual"]["import"]["prepared_documents"] == 1
    assert template.writer.writes == 1
    row["occurrence_count"] = 999
    replica.write_session_records({}, "session_index.json", [row])
    restored.request_run("import_all")
    finish(restored)
    assert template.writer.writes == 1
    row["experiences"][0]["description"] = "更新正文"
    replica.write_session_records({}, "session_index.json", [row])
    restored.request_run("import_all")
    finish(restored)
    assert template.writer.writes == 2 and len(template.writer.content) == 1


def test_pg_large_archive_projects_only_lessons_without_truncating_description(pg_stores):
    from test_successful_experience_import import finish, lesson

    from team_skills.library.experience_import import HISTORY_PATTERN

    store = pg_stores()
    text = "中文成功经验" * 1000
    body = {"session_id": "large", "timestamp": "2026-09-22", "_judge_scores": {
        "skill_experiences": [lesson(text)]}, "messages": [{"content": "private-trajectory" * 150000}]}
    store.put_object("session_archive/large.json", canonical(body))
    source = store.experience_import_sources(phase="objects", pattern=HISTORY_PATTERN,
                                            until=store.database_time())[0]
    assert source["size"] > 1024 * 1024 and "content" not in source
    page = store.experience_import_page(source, offset=0, max_source_bytes=64 * 1024 * 1024)
    assert page["error"] is None and page["total"] == 1
    assert "messages" not in str(page) and "private-trajectory" not in str(page)
    assert page["records"][0]["record"]["description"] == text
    template = worker()
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    w.request_run("import_all")
    finish(w)
    assert w.status()["manual"]["import"]["rejected_sources"] == 0
    assert len(template.writer.content) == 1
    import json
    assert json.loads(next(iter(template.writer.content.values())))["description"] == text


def test_pg_projection_array_and_byte_budgets_and_per_record_gaps(pg_stores):
    from team_skills.library.experience_import import HISTORY_PATTERN

    store = pg_stores()
    entries = [{"session_id": str(n), "evolution_evidence": "exemplary", "evidence_reason": f"经验{n}"}
               for n in range(1005)]
    entries.insert(3, {"session_id": "huge", "evolution_evidence": "exemplary", "evidence_reason": "x" * 300000})
    store.put_object("skill_evidence/large.json", canonical({"skill_name": "review", "evidence": entries}))
    source = store.experience_import_sources(phase="objects", pattern=HISTORY_PATTERN,
                                            until=store.database_time())[0]
    offset, ordinals, gaps = 0, [], []
    while True:
        page = store.experience_import_page(source, offset=offset, max_source_bytes=64 * 1024 * 1024)
        assert len(canonical(page)) <= 1024 * 1024
        assert len(page["records"]) <= 100 and page["error"] is None
        for row in page["records"]:
            ordinals.append(row["ordinal"])
            if row["error"]:
                gaps.append(row)
        offset = page["records"][-1]["ordinal"]
        if offset == page["total"]:
            break
    assert ordinals == list(range(1, 1007))
    assert len(gaps) == 1 and gaps[0]["error"] == "IMPORT_RECORD_TOO_LARGE" and gaps[0]["record"] is None

    from test_successful_experience_import import finish

    template = worker()
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    w.request_run("import_all")
    finish(w)
    report = w.status()["manual"]["import"]
    assert report["prepared_documents"] == 1005 and report["rejected_records"] == 1
    assert report["rejected_sources"] == 1 and w.status()["counts"]["synced"] == 1005
    assert store.object_page(prefix=w.base + "import-pages/") == []


def test_pg_index_projects_only_allowed_fields_and_enforces_page_bytes(pg_stores):
    from test_successful_experience_import import lesson, session

    store = pg_stores()
    meta = session(experiences=[lesson("中" * 70000, experience_key=str(n)) for n in range(10)],
                   private_trace="DO_NOT_RETURN" * 150000)
    store.write_session_records({}, "session_index.json", [meta])
    source = store.experience_import_sources(phase="index", pattern="", until=store.database_time())[0]
    page = store.experience_import_page(source, offset=0, max_source_bytes=64 * 1024 * 1024)
    assert 0 < len(page["records"]) < 10
    assert len(canonical(page)) < 1024 * 1024
    assert "private_trace" not in str(page) and "DO_NOT_RETURN" not in str(page)


def test_pg_import_malformed_and_oversize_sources_continue_with_durable_errors(pg_stores):
    from test_successful_experience_import import finish, lesson

    store = pg_stores()
    template = worker(import_max_source_mb=1)
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    store.put_object("session_archive/bad.json", b"private-invalid-json")
    store.put_object("session_archive/large.json", b"x" * (1024 * 1024 + 1))
    store.put_object("session_archive/good.json", canonical({"session_id": "good", "judge": {
        "skill_experiences": [lesson()]}}))
    w.request_run("import_all")
    finish(w)
    result = w.status()["manual"]["import"]
    assert result["rejected_sources"] == 2 and result["prepared_documents"] == 1
    assert result["error_counts"] == {"INVALID_IMPORT_SOURCE": 1, "IMPORT_SOURCE_TOO_LARGE": 1}
    assert len(result["errors_sample"]) == 2 and "private-invalid-json" not in str(result)
    large = next(e for e in result["errors_sample"] if e["code"] == "IMPORT_SOURCE_TOO_LARGE")
    assert large["source_bytes"] == 1048577 and large["limit_bytes"] == 1048576
    assert len(store.object_page(prefix=w.base + "import-errors/")) == 2
    assert template.writer.writes == 1


@pytest.mark.parametrize("count", [1, 2])
def test_pg_source_change_before_confirmation_cannot_reach_candidate_groups(pg_stores, monkeypatch, count):
    from test_successful_experience_import import finish, lesson

    store, concurrent = pg_stores(), pg_stores()
    key = "session_archive/changing.json"
    value = {"session_id": "changing", "judge": {"skill_experiences": [
        lesson("first"), lesson("second", experience_key="b")][:count]}}
    store.put_object(key, canonical(value))
    template = worker(batch_size=1)
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    original = store.experience_import_page
    changed = False

    def change(source, **kwargs):
        nonlocal changed
        result = original(source, **kwargs)
        if not changed:
            changed = True
            value["judge"]["skill_experiences"][0]["description"] = "newer"
            concurrent.put_object(key, canonical(value))
        return result

    monkeypatch.setattr(store, "experience_import_page", change)
    w.request_run("import_all")
    finish(w)
    assert w.status()["manual"]["import"]["last_error"] == "IMPORT_SOURCE_CHANGED"
    assert store.object_page(prefix=w.base + "import-groups/") == []
    assert store.object_page(prefix=w.base + "import-pages/") == []
    assert template.writer.writes == 0
    w.request_run("import_all")
    finish(w)
    assert template.writer.writes == count


def test_pg_error_samples_bounded_and_full_failures_retained(pg_stores):
    from test_successful_experience_import import finish, lesson, session

    store = pg_stores()
    row = session(experiences=[lesson("", experience_key=str(n)) for n in range(25)] + [lesson()])
    # Irrelevant giant defect text must not cause a gap or leave PG.
    row["experiences"].append(lesson("x" * 300000, kind="defect"))
    store.write_session_records({}, "session_index.json", [row])
    template = worker()
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    w.request_run("import_all")
    finish(w)
    report = w.status()["manual"]["import"]
    assert report["rejected_sources"] == 1 and report["rejected_records"] == 25
    assert report["error_counts"] == {"INVALID_IMPORT_RECORD": 25}
    assert len(report["errors_sample"]) == 20
    assert len(store.object_page(prefix=w.base + "import-errors/")) == 25
    assert w.status()["counts"]["synced"] == 1


def test_pg_source_private_pages_resume_after_restart_without_early_upload(pg_stores):
    from test_successful_experience_import import finish, lesson, session

    store, replica = pg_stores(), pg_stores()
    meta = session(experiences=[lesson(experience_key=str(n)) for n in range(3)])
    store.write_session_records({}, "session_index.json", [meta])
    template = worker(batch_size=1)
    w = ExperienceSync(store, template.target, template.settings, template.writer)
    w.request_run("import_all")
    for _ in range(10):
        w.run_once()
        if w.status()["manual"]["import"].get("source", {}).get("offset") == 1:
            break
    else:
        pytest.fail("no durable intermediate page")
    assert len(store.object_page(prefix=w.base + "import-pages/")) == 1
    assert store.object_page(prefix=w.base + "import-groups/") == []
    assert template.writer.writes == 0
    restored = ExperienceSync(replica, template.target, template.settings, template.writer)
    finish(restored)
    assert template.writer.writes == 3 and len(template.writer.content) == 3
    assert store.object_page(prefix=w.base + "import-pages/") == []
