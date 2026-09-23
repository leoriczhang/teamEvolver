"""Read-only PostgreSQL projection for the explicit experience importer.

No schema changes or JSON functions are installed. Each query parses at most
one size-bounded source, under tenant RLS, and returns only selected fields.
"""

import json

PAGE_BYTES = 1024 * 1024
RECORD_BYTES = 256 * 1024


def query_timeout(store):
    return min(30.0, float(store._command_timeout or 30), store._op_timeout)


def sources_page(store, *, phase, pattern, until, limit=100, after_time="-infinity", after_key="",
                 after_index="", after_session=""):
    async def read():
        async with store._runtime.tenant_conn(store.tenant_id) as conn:
            if phase == "objects":
                rows = await conn.fetch(
                    f"SELECT key,updated_at,xmin::text AS revision,octet_length(content) AS size "
                    f"FROM {store._schema}.objects WHERE tenant_id=$1 AND key ~ $2 "
                    "AND (updated_at,key)>($3::text::timestamptz,$4) AND updated_at<=$5::text::timestamptz "
                    "ORDER BY updated_at,key LIMIT $6",
                    store.tenant_id, pattern, after_time, after_key, until, max(1, min(100, limit)),
                    timeout=query_timeout(store),
                )
            else:
                rows = await conn.fetch(
                    f"SELECT index_key AS key,session_id,updated_at,xmin::text AS revision,"
                    f"octet_length(meta::text) AS size FROM {store._schema}.session_index WHERE tenant_id=$1 "
                    "AND (index_key,session_id)>($2,$3) AND updated_at<=$4::text::timestamptz "
                    "ORDER BY index_key,session_id LIMIT $5",
                    store.tenant_id, after_index, after_session, until, max(1, min(100, limit)),
                    timeout=query_timeout(store),
                )
        return [{**dict(row), "phase": phase, "updated_at": row["updated_at"].isoformat()} for row in rows]
    return store._runtime.run(read(), timeout=query_timeout(store))


def _source_sql(store, source):
    if source["phase"] == "objects":
        return (
            f"SELECT content,octet_length(content) AS size FROM {store._schema}.objects "
            "WHERE tenant_id=$1 AND key=$2 AND xmin::text=$3 AND updated_at=$4::text::timestamptz",
            [store.tenant_id, source["key"], source["revision"], source["updated_at"]],
            "convert_from(content,'UTF8')::jsonb",
        )
    return (
        f"SELECT meta,octet_length(meta::text) AS size FROM {store._schema}.session_index "
        "WHERE tenant_id=$1 AND index_key=$2 AND xmin::text=$3 AND updated_at=$4::text::timestamptz "
        "AND session_id=$5",
        [store.tenant_id, source["key"], source["revision"], source["updated_at"], source["session_id"]],
        "meta",
    )


def source_unchanged(store, source):
    import asyncpg

    sql, args, _ = _source_sql(store, source)

    async def read():
        async with store._runtime.tenant_conn(store.tenant_id) as conn:
            return await conn.fetchval(f"SELECT EXISTS({sql})", *args, timeout=query_timeout(store))
    try:
        return store._runtime.run(read(), timeout=query_timeout(store))
    except asyncpg.QueryCanceledError:
        raise TimeoutError() from None


def project_page(store, source, *, offset, max_source_bytes, limit=100):
    import asyncpg

    if source["size"] > max_source_bytes:
        return {"error": "IMPORT_SOURCE_TOO_LARGE", "limit_bytes": max_source_bytes}
    sql, args, body = _source_sql(store, source)
    n = len(args)
    cap, start, count = f"${n+1}", f"${n+2}", f"${n+3}"
    key = "/" + source["key"]
    evidence = "/skill_evidence/" in key
    archive = source["phase"] == "objects" and not evidence and "/experience_library/sessions/" not in key
    # Preserve the existing public-judge precedence without returning scores or trajectories.
    if evidence:
        array = "body->'evidence'"
        invalid = "false"
    elif archive:
        scores = ("CASE WHEN jsonb_typeof(body->'judge')='object' AND (body->'judge') ? 'skill_experiences' "
                  "THEN body->'judge' WHEN jsonb_typeof(body->'_judge_scores')='object' "
                  "AND body->'_judge_scores'<>'{}'::jsonb THEN body->'_judge_scores' "
                  "ELSE body->'judge' END")
        array = f"({scores})->'skill_experiences'"
        invalid = ("EXISTS(SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(body)='object' THEN body "
                   "ELSE '{}'::jsonb END) f WHERE f.key IN ('judge','_judge_scores') "
                   "AND jsonb_typeof(f.value) NOT IN ('object','null'))")
    else:
        array = "body->'experiences'"
        invalid = "NOT (body ? 'experiences')"
    if evidence:
        projection = """jsonb_build_object(
            'skill_name',body->'skill_name','session_id',entry->'session_id',
            'timestamp',entry->'timestamp','ingested_at',entry->'ingested_at',
            'evolution_evidence',entry->'evolution_evidence','evidence_reason',entry->'evidence_reason')"""
    else:
        projection = """jsonb_build_object(
            'session_id',body->'session_id','timestamp',body->'timestamp','ingested_at',body->'ingested_at',
            'skill_name',entry->'skill_name','kind',entry->'kind',
            'experience_key',entry->'experience_key','description',entry->'description')"""
    if evidence:
        eligible = "lower(btrim(COALESCE(entry->>'evolution_evidence','')))='exemplary'"
    elif archive:
        eligible = "lower(btrim(COALESCE(entry->>'kind','')))='exemplary'"
    else:
        eligible = "COALESCE(entry->>'kind','')='exemplary'"
    # The materialized source guards the cast by source size. Budgeting happens
    # in PG, before returning data; a giant individual description is never sent.
    query = f"""
        WITH source AS MATERIALIZED ({sql}),
        decoded AS MATERIALIZED (SELECT size,CASE WHEN size<={cap} THEN {body} END AS body FROM source),
        shaped AS MATERIALIZED (SELECT *,{array} AS entries,
            CASE WHEN size>{cap} THEN 'IMPORT_SOURCE_TOO_LARGE'
                 WHEN jsonb_typeof(body) IS DISTINCT FROM 'object' OR {invalid} THEN 'INVALID_IMPORT_SOURCE'
                 WHEN {array} IS NOT NULL AND jsonb_typeof({array}) NOT IN ('array','null')
                 THEN 'INVALID_IMPORT_SOURCE' END AS error FROM decoded),
        page AS MATERIALIZED (
            SELECT ordinal,entry,body FROM shaped,
            LATERAL jsonb_array_elements(CASE WHEN error IS NULL AND jsonb_typeof(entries)='array'
                THEN entries ELSE '[]'::jsonb END) WITH ORDINALITY AS e(entry,ordinal)
            WHERE ordinal>{start} ORDER BY ordinal LIMIT {count}),
        projected AS MATERIALIZED (SELECT ordinal,
            jsonb_typeof(entry)='object' AND NOT ({eligible}) AS skip,
            CASE WHEN jsonb_typeof(entry)='object' AND ({eligible}) THEN {projection} END AS record FROM page),
        sized AS (SELECT *,octet_length(record::text) AS record_bytes FROM projected),
        budgeted AS (SELECT *,SUM(LEAST(COALESCE(record_bytes,0),{RECORD_BYTES})+256)
            OVER (ORDER BY ordinal) AS budget FROM sized)
        SELECT size,error,CASE WHEN jsonb_typeof(entries)='array' THEN jsonb_array_length(entries) ELSE 0 END AS total,
            COALESCE((SELECT jsonb_agg(jsonb_build_object('ordinal',ordinal,'record_bytes',record_bytes,
                'record',CASE WHEN record_bytes<={RECORD_BYTES} THEN record END,'skip',skip,
                'error',CASE WHEN skip THEN NULL WHEN record IS NULL THEN 'INVALID_IMPORT_RECORD'
                    WHEN record_bytes>{RECORD_BYTES} THEN 'IMPORT_RECORD_TOO_LARGE' END) ORDER BY ordinal)
                FROM budgeted WHERE budget<={PAGE_BYTES-1024}), '[]'::jsonb) AS records
        FROM shaped
    """

    async def read():
        async with store._runtime.tenant_conn(store.tenant_id) as conn:
            row = await conn.fetchrow(query, *args, max_source_bytes, offset, max(1, min(100, limit)),
                                      timeout=query_timeout(store))
        if row is None:
            return {"error": "IMPORT_SOURCE_CHANGED"}
        value = dict(row)
        value["records"] = json.loads(value["records"]) if isinstance(value["records"], str) else value["records"]
        return value
    try:
        return store._runtime.run(read(), timeout=query_timeout(store))
    except (TimeoutError, asyncpg.QueryCanceledError):
        return {"error": "IMPORT_QUERY_TIMEOUT"}
    except (asyncpg.DataError, asyncpg.ProgramLimitExceededError):
        return {"error": "INVALID_IMPORT_SOURCE"}
