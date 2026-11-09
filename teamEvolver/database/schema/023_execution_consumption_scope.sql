-- Pure Prompt profiles independently consume matching Trace Evidence from a
-- shared archived Session. Existing rows retain the legacy Skill scope.
ALTER TABLE {schema}.skill_evolution_sessions
    ADD COLUMN consumption_scope TEXT NOT NULL DEFAULT '';

ALTER TABLE {schema}.skill_evolution_sessions
    DROP CONSTRAINT skill_evolution_sessions_pkey;

ALTER TABLE {schema}.skill_evolution_sessions
    ADD PRIMARY KEY (tenant_id, consumption_scope, session_id, content_hash);
