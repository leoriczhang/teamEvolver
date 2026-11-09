-- Prompt-only changes use the existing Candidate lifecycle without a fabricated Skill.
ALTER TABLE {schema}.skill_candidates ALTER COLUMN skill_name DROP NOT NULL;
ALTER TABLE {schema}.skill_candidates ADD COLUMN candidate_kind TEXT NOT NULL DEFAULT 'skill';
ALTER TABLE {schema}.skill_candidates ADD COLUMN execution_profile_id TEXT NOT NULL DEFAULT '';
ALTER TABLE {schema}.skill_candidates ADD CONSTRAINT skill_candidates_subject_check CHECK (
    (candidate_kind = 'skill' AND skill_name IS NOT NULL AND execution_profile_id = '')
    OR (candidate_kind = 'execution' AND skill_name IS NULL AND execution_profile_id <> '' AND base_version = 0)
);
CREATE UNIQUE INDEX skill_candidates_live_execution_idx
    ON {schema}.skill_candidates(tenant_id, execution_profile_id)
    WHERE candidate_kind = 'execution' AND status IN ('open', 'validating');
