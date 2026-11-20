-- Automatic eligibility is separate from immutable historical inputs.
ALTER TABLE {schema}.skill_evidence ADD COLUMN archived_at TIMESTAMPTZ;
ALTER TABLE {schema}.skill_evidence ADD COLUMN archive_reason TEXT NOT NULL DEFAULT '';
CREATE TABLE {schema}.dataset_case_archives (
    tenant_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    case_id TEXT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reason TEXT NOT NULL,
    fingerprint JSONB NOT NULL,
    PRIMARY KEY (tenant_id,dataset_id,revision,case_id),
    FOREIGN KEY (tenant_id,dataset_id,revision,case_id)
        REFERENCES {schema}.dataset_cases(tenant_id,dataset_id,revision,case_id)
);
ALTER TABLE {schema}.dataset_case_archives ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.dataset_case_archives FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.dataset_case_archives
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
