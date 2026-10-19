-- Experience source records, grounded Patterns and durable projection intents.
CREATE TABLE {schema}.skill_experiences (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    experience_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('defect', 'exemplary')),
    experience_key TEXT NOT NULL,
    document JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, experience_id),
    UNIQUE (tenant_id, skill_name, kind, experience_key)
);
CREATE TABLE {schema}.experience_occurrences (
    tenant_id TEXT NOT NULL,
    experience_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_alias TEXT NOT NULL,
    description TEXT NOT NULL,
    score DOUBLE PRECISION,
    observed_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, experience_id, session_id),
    FOREIGN KEY (tenant_id, experience_id)
        REFERENCES {schema}.skill_experiences(tenant_id, experience_id)
);
CREATE TABLE {schema}.experience_pattern_compilations (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    skill_name TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    revision INTEGER NOT NULL,
    compiled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, skill_name)
);
CREATE TABLE {schema}.experience_patterns (
    tenant_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('defect', 'exemplary')),
    status TEXT NOT NULL CHECK (status IN ('ready', 'observing')),
    document JSONB NOT NULL,
    PRIMARY KEY (tenant_id, skill_name, pattern_id),
    FOREIGN KEY (tenant_id, skill_name)
        REFERENCES {schema}.experience_pattern_compilations(tenant_id, skill_name)
);
CREATE TABLE {schema}.experience_commands (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    command_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('sync', 'import_all', 'compile', 'deliver')),
    request_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    checkpoint JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, command_id)
);
CREATE TABLE {schema}.experience_sync_outbox (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    event_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('experience', 'patterns')),
    subject_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    document JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'queued', 'synced', 'dead_letter')),
    command_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    content_written BOOLEAN NOT NULL DEFAULT false,
    semantic_status TEXT NOT NULL DEFAULT 'unknown',
    vector_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, subject_type, subject_id, revision),
    FOREIGN KEY (tenant_id, command_id)
        REFERENCES {schema}.experience_commands(tenant_id, command_id)
);
CREATE TABLE {schema}.experience_scan_state (
    tenant_id TEXT PRIMARY KEY REFERENCES {schema}.tenants(tenant_id),
    watermark TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX experience_list_idx ON {schema}.skill_experiences(tenant_id, skill_name, kind, updated_at);
CREATE INDEX experience_occurrence_recent_idx
    ON {schema}.experience_occurrences(tenant_id, experience_id, observed_at DESC, session_id DESC);
CREATE INDEX experience_outbox_pending_idx
    ON {schema}.experience_sync_outbox(tenant_id, created_at, event_id) WHERE status='pending';
CREATE INDEX experience_command_recent_idx ON {schema}.experience_commands(tenant_id, created_at DESC);
DO $ddl$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'skill_experiences', 'experience_occurrences', 'experience_pattern_compilations',
        'experience_patterns', 'experience_commands', 'experience_sync_outbox', 'experience_scan_state'
    ] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON {schema}.%I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))', t);
    END LOOP;
END
$ddl$;
