-- Skill mining metadata and artifact bytes. Local files are rebuildable caches.
CREATE TABLE IF NOT EXISTS {schema}.skill_mining_jobs (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    files_revision BIGINT NOT NULL DEFAULT 0 CHECK (files_revision >= 0),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,job_id)
);
CREATE INDEX IF NOT EXISTS skill_mining_jobs_status_idx
    ON {schema}.skill_mining_jobs(tenant_id,status,updated_at DESC) WHERE NOT deleted;

CREATE TABLE IF NOT EXISTS {schema}.skill_mining_sources (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    account TEXT NOT NULL,
    uri TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,source_id),
    UNIQUE(tenant_id,account,uri)
);

CREATE TABLE IF NOT EXISTS {schema}.skill_mining_files (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size BIGINT NOT NULL CHECK (size >= 0),
    content BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,job_id,path),
    FOREIGN KEY(tenant_id,job_id) REFERENCES {schema}.skill_mining_jobs(tenant_id,job_id) ON DELETE CASCADE,
    CHECK (octet_length(content) = size)
);

CREATE TABLE IF NOT EXISTS {schema}.skill_mining_settings (
    tenant_id TEXT PRIMARY KEY REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    document JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(document) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE {schema}.skill_mining_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_sources FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_files FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_mining_settings FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON {schema}.skill_mining_jobs
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
CREATE POLICY tenant_isolation ON {schema}.skill_mining_sources
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
CREATE POLICY tenant_isolation ON {schema}.skill_mining_files
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
CREATE POLICY tenant_isolation ON {schema}.skill_mining_settings
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
