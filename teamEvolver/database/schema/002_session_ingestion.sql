-- TeamEvolver Session Ingestion schema (plan Phase 4).
--
-- Applied by `teamEvolver schema apply` after 001_init.sql. The literal
-- `{schema}` is replaced with the configured schema name at apply time. This
-- file is immutable once recorded in schema_versions.
--
-- Scope (plan Phase 4): the persistent async acceptance link that unifies Push
-- (`/ingest_session`) and Pull (`/api/datasource*`). Three tenant-scoped tables,
-- all with ENABLE+FORCE RLS and tenant_id as the leading PK column:
--
--   ingestions           acceptance records (one per accepted Push/Pull item);
--                        the API writes this + a raw payload artifact + a
--                        worker_tasks row in one short transaction, then returns
--                        202. `dedupe_key` makes a repeated submit idempotent.
--   session_stage_results per-stage outcome (normalize / analyze / trace_analyze
--                        / archive). Each stage writes its own row; a formal
--                        Session is written only after every mandatory stage
--                        succeeds (plan Phase 4 exit).
--   sessions             the formal Session, written atomically after mandatory
--                        analysis succeeds. Carries BOTH the business occurrence
--                        time (`business_time`) and the ingest/sync time
--                        (`ingested_at`) — plan Phase 4 exit "同时保存业务发生
--                        时间和入库时间".
--
-- Adapter definitions / immutable versions / tenant bindings already exist in
-- 001_init.sql (adapters, adapter_versions, adapter_bindings); the session
-- ingestion adapters/repository.py reads/writes those.

-- --------------------------------------------------------------------------- --
-- Ingestion acceptance records.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.ingestions (
    tenant_id      TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    ingestion_id   TEXT NOT NULL,
    source         TEXT NOT NULL
        CHECK (source IN ('push', 'pull')),
    external_id    TEXT NOT NULL DEFAULT '',
    user_id        TEXT NOT NULL DEFAULT '',
    account_id     TEXT NOT NULL DEFAULT '',
    dedupe_key     TEXT NOT NULL DEFAULT '',
    task_id        TEXT NOT NULL DEFAULT '',
    adapter_id     TEXT NOT NULL DEFAULT '',
    adapter_revision BIGINT NOT NULL DEFAULT 0,
    config_revision  BIGINT NOT NULL DEFAULT 0,
    payload_ref    TEXT NOT NULL DEFAULT '',
    payload_hash   TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'processing', 'archived', 'skipped',
                          'duplicate', 'failed')),
    business_time  TIMESTAMPTZ,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ingestion_id)
);

-- At most one live ingestion per (tenant, dedupe_key): a repeated submit maps
-- back to the first acceptance (plan §11 "重复提交幂等").
CREATE UNIQUE INDEX IF NOT EXISTS ingestions_dedupe_idx
    ON {schema}.ingestions (tenant_id, dedupe_key)
    WHERE dedupe_key <> '';

CREATE INDEX IF NOT EXISTS ingestions_tenant_time_idx
    ON {schema}.ingestions (tenant_id, received_at DESC);

ALTER TABLE {schema}.ingestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.ingestions FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'ingestions'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.ingestions
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Per-stage results. Mandatory stages must all succeed before a formal Session
-- is written; a failed stage leaves no formal Session or Evidence (plan Phase 4
-- exit "强制分析失败不产生正式 Session 或 Evidence").
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.session_stage_results (
    tenant_id      TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    ingestion_id   TEXT NOT NULL,
    stage          TEXT NOT NULL
        CHECK (stage IN ('normalize', 'analyze', 'trace_analyze', 'archive')),
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed')),
    result_ref     TEXT NOT NULL DEFAULT '',
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code     TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ingestion_id, stage),
    FOREIGN KEY (tenant_id, ingestion_id)
        REFERENCES {schema}.ingestions (tenant_id, ingestion_id) ON DELETE CASCADE
);

ALTER TABLE {schema}.session_stage_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.session_stage_results FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'session_stage_results'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.session_stage_results
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Formal Sessions. Written atomically only after every mandatory stage
-- succeeds. Carries both the business occurrence time and the ingest time.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.sessions (
    tenant_id      TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    session_id     TEXT NOT NULL,
    ingestion_id   TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    external_id    TEXT NOT NULL DEFAULT '',
    user_id        TEXT NOT NULL DEFAULT '',
    account_id     TEXT NOT NULL DEFAULT '',
    decision       TEXT NOT NULL DEFAULT ''
        CHECK (decision IN ('', 'valuable', 'skipped')),
    content_ref    TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL DEFAULT '',
    judge          JSONB NOT NULL DEFAULT '{}'::jsonb,
    filter_audit   JSONB NOT NULL DEFAULT '{}'::jsonb,
    consumption    JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta           JSONB NOT NULL DEFAULT '{}'::jsonb,
    business_time  TIMESTAMPTZ,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, session_id)
);

CREATE INDEX IF NOT EXISTS sessions_tenant_business_time_idx
    ON {schema}.sessions (tenant_id, business_time DESC);

CREATE INDEX IF NOT EXISTS sessions_tenant_ingested_idx
    ON {schema}.sessions (tenant_id, ingested_at DESC);

ALTER TABLE {schema}.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.sessions FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'sessions'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.sessions
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;
