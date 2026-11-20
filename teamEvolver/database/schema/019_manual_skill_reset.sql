-- A manual library change starts a new automatic Evidence window.
ALTER TABLE {schema}.skills ADD COLUMN evolution_reset_at TIMESTAMPTZ;
ALTER TABLE {schema}.skill_candidates DROP CONSTRAINT skill_candidates_status_check;
ALTER TABLE {schema}.skill_candidates ADD CONSTRAINT skill_candidates_status_check
    CHECK (status IN ('open', 'validating', 'published', 'rejected', 'archived'));
