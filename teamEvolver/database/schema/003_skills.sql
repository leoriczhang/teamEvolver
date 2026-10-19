-- TeamEvolver Skill library + candidate schema (plan Phase 5.1).
--
-- Applied by `teamEvolver schema apply` after 002_session_ingestion.sql. The
-- literal `{schema}` is replaced with the configured schema name at apply time.
-- This file is immutable once recorded in schema_versions.
--
-- Scope (plan Phase 5.1): PostgreSQL becomes the single authority for Skill,
-- version, manifest and Candidate state. The local `skills_dir` is only a
-- rebuildable runtime cache materialised from these rows. Every table is
-- tenant-scoped with ENABLE+FORCE RLS and tenant_id as the leading PK column.
--
--   skills                 the manifest head: one row per (tenant, skill name),
--                          pointing at the current version (or a tombstone).
--   skill_versions         append-only version history (publish / update /
--                          rollback / delete tombstone). Never rewritten.
--   skill_blobs            content-addressed, immutable file bytes (sha256).
--                          Shared by versions and candidates; deduplicated.
--   skill_version_files    the file list of one version -> blob.
--   skill_mutations        one row per mutation id: the idempotency ledger. A
--                          repeated mutation id replays its first result and
--                          never produces a second version.
--   skill_candidates       evolution candidates (status + verdict + human marks).
--   skill_candidate_files  the current revision's bundle of a candidate.
--   skill_candidate_events auditable candidate history (created / revised /
--                          transition / feedback / verdict).
--
-- Version, version files, blobs and the mutation row are written in ONE
-- transaction by team_skills/library/repository.py, so a reader never sees a
-- version without its files or a mutation without its version.

-- --------------------------------------------------------------------------- --
-- Manifest head.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.skills (
    tenant_id       TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    name            TEXT NOT NULL
        CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'),
    skill_id        TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    description     TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    sha256          TEXT NOT NULL DEFAULT '',
    tree_sha256     TEXT NOT NULL DEFAULT '',
    runtime_policy  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name)
);

-- --------------------------------------------------------------------------- --
-- Append-only version history.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.skill_versions (
    tenant_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL CHECK (version > 0),
    action         TEXT NOT NULL
        CHECK (action IN ('publish', 'update', 'rollback', 'delete')),
    deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    sha256         TEXT NOT NULL DEFAULT '',
    tree_sha256    TEXT NOT NULL DEFAULT '',
    file_count     INTEGER NOT NULL DEFAULT 0,
    total_bytes    BIGINT NOT NULL DEFAULT 0,
    description    TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT 'general',
    runtime_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    restored_from  INTEGER,
    mutation_id    TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name, version),
    FOREIGN KEY (tenant_id, name)
        REFERENCES {schema}.skills (tenant_id, name) ON DELETE CASCADE
);

-- --------------------------------------------------------------------------- --
-- Content-addressed, immutable file bytes.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.skill_blobs (
    tenant_id  TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    sha256     TEXT NOT NULL,
    size       BIGINT NOT NULL CHECK (size >= 0),
    content    BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sha256)
);

CREATE TABLE IF NOT EXISTS {schema}.skill_version_files (
    tenant_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    version   INTEGER NOT NULL,
    path      TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    size      BIGINT NOT NULL CHECK (size >= 0),
    PRIMARY KEY (tenant_id, name, version, path),
    FOREIGN KEY (tenant_id, name, version)
        REFERENCES {schema}.skill_versions (tenant_id, name, version) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, sha256)
        REFERENCES {schema}.skill_blobs (tenant_id, sha256) ON DELETE RESTRICT
);

-- --------------------------------------------------------------------------- --
-- Mutation idempotency ledger.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.skill_mutations (
    tenant_id    TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    mutation_id  TEXT NOT NULL,
    action       TEXT NOT NULL
        CHECK (action IN ('publish', 'update', 'rollback', 'delete')),
    name         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'unchanged')),
    version      INTEGER NOT NULL DEFAULT 0,
    previous_version INTEGER NOT NULL DEFAULT 0,
    request_hash TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mutation_id)
);

CREATE INDEX IF NOT EXISTS skill_mutations_name_idx
    ON {schema}.skill_mutations (tenant_id, name, created_at DESC);

-- --------------------------------------------------------------------------- --
-- Evolution candidates.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS {schema}.skill_candidates (
    tenant_id         TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    candidate_id      TEXT NOT NULL,
    skill_name        TEXT NOT NULL
        CHECK (skill_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'),
    proposed_action   TEXT NOT NULL DEFAULT 'update'
        CHECK (proposed_action IN ('create', 'update')),
    status            TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'validating', 'published', 'rejected')),
    verdict           TEXT NOT NULL DEFAULT ''
        CHECK (verdict IN ('', 'accept', 'reject', 'inconclusive')),
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    base_version      INTEGER NOT NULL DEFAULT 0 CHECK (base_version >= 0),
    tree_sha256       TEXT NOT NULL DEFAULT '',
    reviewed          BOOLEAN NOT NULL DEFAULT FALSE,
    adopted           BOOLEAN NOT NULL DEFAULT FALSE,
    rejected          BOOLEAN NOT NULL DEFAULT FALSE,
    published_version INTEGER NOT NULL DEFAULT 0,
    mutation_id       TEXT NOT NULL DEFAULT '',
    validate_task_id  TEXT NOT NULL DEFAULT '',
    detail            JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluation        JSONB NOT NULL DEFAULT '{}'::jsonb,
    decision          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at        TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, candidate_id)
);

-- At most one live (open / validating) candidate per skill: a later evolution
-- cycle revises it in place instead of racing a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS skill_candidates_live_skill_idx
    ON {schema}.skill_candidates (tenant_id, skill_name)
    WHERE status IN ('open', 'validating');

CREATE INDEX IF NOT EXISTS skill_candidates_status_idx
    ON {schema}.skill_candidates (tenant_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.skill_candidate_files (
    tenant_id    TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size         BIGINT NOT NULL CHECK (size >= 0),
    PRIMARY KEY (tenant_id, candidate_id, path),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES {schema}.skill_candidates (tenant_id, candidate_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, sha256)
        REFERENCES {schema}.skill_blobs (tenant_id, sha256) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS {schema}.skill_candidate_events (
    tenant_id    TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    seq          INTEGER NOT NULL CHECK (seq > 0),
    kind         TEXT NOT NULL
        CHECK (kind IN ('created', 'revised', 'transition', 'feedback', 'verdict')),
    from_status  TEXT NOT NULL DEFAULT '',
    to_status    TEXT NOT NULL DEFAULT '',
    revision     INTEGER NOT NULL DEFAULT 1,
    actor        TEXT NOT NULL DEFAULT '',
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, candidate_id, seq),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES {schema}.skill_candidates (tenant_id, candidate_id) ON DELETE CASCADE
);

-- --------------------------------------------------------------------------- --
-- Row-Level Security: fail closed when app.tenant_id is empty or mismatched.
-- --------------------------------------------------------------------------- --
DO $ddl$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'skills', 'skill_versions', 'skill_blobs', 'skill_version_files',
        'skill_mutations', 'skill_candidates', 'skill_candidate_files',
        'skill_candidate_events'
    ] LOOP
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
