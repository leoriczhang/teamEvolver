-- TeamEvolver platform schema — initial version.
--
-- Applied by `teamEvolver schema apply` via teamEvolver/database/schema_runner.py.
-- The literal `{schema}` is replaced with the configured schema name at apply
-- time. This file is immutable once recorded in schema_versions; further
-- changes go into new NNN_*.sql files.
--
-- Scope (plan Phase 1 / 4.4): tenants, global config, per-tenant module config,
-- secret references, adapters, worker task queue, resource leases, tenant usage,
-- artifacts and audit. All tenant-scoped tables enable+force RLS and fail closed
-- when app.tenant_id is empty or mismatched.

-- --------------------------------------------------------------------------- --
-- Tenants (control plane; not RLS-scoped — the registry reads it to resolve
-- identity before a tenant GUC exists).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.tenants (
    tenant_id        TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    agent_token_hash TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    config           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO {schema}.tenants (tenant_id, display_name)
VALUES ('default', 'Default (single-tenant compatibility)')
ON CONFLICT (tenant_id) DO NOTHING;

-- --------------------------------------------------------------------------- --
-- Global configuration: exactly one row, CAS-guarded by `revision`.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.teamevolver_config (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id = TRUE),
    revision    BIGINT NOT NULL DEFAULT 1,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO {schema}.teamevolver_config (id, revision, config)
VALUES (TRUE, 1, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------- --
-- Per-tenant, per-module configuration. PK carries tenant_id (plan 5.3).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.tenant_configs (
    tenant_id   TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    module      TEXT NOT NULL,
    revision    BIGINT NOT NULL DEFAULT 1,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, module)
);

ALTER TABLE {schema}.tenant_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_configs FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'tenant_configs'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.tenant_configs
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Secret references. Only an opaque ref + encrypted material + metadata; the
-- plaintext never lands in PG, logs, trace or task payloads (plan 5.3).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.secret_refs (
    tenant_id   TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    secret_ref  TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT '',
    ciphertext  TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, secret_ref)
);

ALTER TABLE {schema}.secret_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.secret_refs FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'secret_refs'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.secret_refs
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Adapter definitions, immutable source versions, and tenant bindings.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.adapters (
    tenant_id   TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    adapter_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, adapter_id)
);

CREATE TABLE IF NOT EXISTS {schema}.adapter_versions (
    tenant_id   TEXT NOT NULL,
    adapter_id  TEXT NOT NULL,
    revision    BIGINT NOT NULL,
    source_hash TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, adapter_id, revision),
    FOREIGN KEY (tenant_id, adapter_id)
        REFERENCES {schema}.adapters (tenant_id, adapter_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS {schema}.adapter_bindings (
    tenant_id   TEXT NOT NULL,
    binding_key TEXT NOT NULL,
    adapter_id  TEXT NOT NULL,
    revision    BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, binding_key),
    FOREIGN KEY (tenant_id, adapter_id, revision)
        REFERENCES {schema}.adapter_versions (tenant_id, adapter_id, revision) ON DELETE RESTRICT
);

DO $ddl$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['adapters', 'adapter_versions', 'adapter_bindings'] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = '{schema}' AND tablename = t AND policyname = 'tenant_isolation'
        ) THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON {schema}.%I '
                'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
                'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
                t
            );
        END IF;
    END LOOP;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Worker task queue (plan 5.2). Full lifecycle fields; claimed with
-- FOR UPDATE SKIP LOCKED and fenced writes by the Phase 2 runtime.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.worker_tasks (
    tenant_id        TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    task_id          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    subject_type     TEXT NOT NULL DEFAULT '',
    subject_id       TEXT NOT NULL DEFAULT '',
    schema_version   TEXT NOT NULL DEFAULT '',
    dedupe_key       TEXT NOT NULL DEFAULT '',
    payload_ref      TEXT NOT NULL DEFAULT '',
    payload_hash     TEXT NOT NULL DEFAULT '',
    config_revision  BIGINT NOT NULL DEFAULT 0,
    adapter_revision BIGINT NOT NULL DEFAULT 0,
    trace_id         TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retry_wait', 'waiting_external',
                          'reconciling', 'succeeded', 'failed', 'cancelled')),
    attempt          INTEGER NOT NULL DEFAULT 0,
    owner_id         TEXT NOT NULL DEFAULT '',
    fence            BIGINT NOT NULL DEFAULT 0,
    lease_until      TIMESTAMPTZ,
    available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    upstream_task_id TEXT NOT NULL DEFAULT '',
    result_ref       TEXT NOT NULL DEFAULT '',
    error_code       TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, task_id)
);

-- Fair claim scan: ready tasks ordered by availability.
CREATE INDEX IF NOT EXISTS worker_tasks_claim_idx
    ON {schema}.worker_tasks (status, available_at)
    WHERE status IN ('queued', 'retry_wait');

-- Dedupe: at most one live task per (tenant, kind, dedupe_key).
CREATE UNIQUE INDEX IF NOT EXISTS worker_tasks_dedupe_idx
    ON {schema}.worker_tasks (tenant_id, kind, dedupe_key)
    WHERE dedupe_key <> '' AND status NOT IN ('succeeded', 'failed', 'cancelled');

ALTER TABLE {schema}.worker_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.worker_tasks FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'worker_tasks'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.worker_tasks
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Resource leases: cross-replica exclusivity for tenant concurrency and
-- exclusive targets such as Memory directories and experience streams.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.resource_leases (
    tenant_id    TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    resource_key TEXT NOT NULL,
    owner_id     TEXT NOT NULL DEFAULT '',
    fence        BIGINT NOT NULL DEFAULT 0,
    lease_until  TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, resource_key)
);

ALTER TABLE {schema}.resource_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.resource_leases FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'resource_leases'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.resource_leases
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Tenant usage: logical task accounting; retries do not double-charge (plan 5.2).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.tenant_usage (
    tenant_id   TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    metric      TEXT NOT NULL,
    window_key  TEXT NOT NULL,
    counter     BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, metric, window_key)
);

ALTER TABLE {schema}.tenant_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_usage FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'tenant_usage'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.tenant_usage
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Artifacts: metadata + content location only (payload lives in ObjectStore).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.artifacts (
    tenant_id    TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    artifact_id  TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT '',
    content_ref  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, artifact_id)
);

ALTER TABLE {schema}.artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.artifacts FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'artifacts'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.artifacts
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$;

-- --------------------------------------------------------------------------- --
-- Audit log: append-only platform record (control-plane, not RLS-scoped so the
-- console can read across tenants; tenant_id is still carried on every row).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_tenant_time_idx
    ON {schema}.audit_log (tenant_id, created_at DESC);
