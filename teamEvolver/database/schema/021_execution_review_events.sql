-- Independent verification is a separate audited declaration, not a feedback mark.
ALTER TABLE {schema}.skill_candidate_events DROP CONSTRAINT skill_candidate_events_kind_check;
ALTER TABLE {schema}.skill_candidate_events ADD CONSTRAINT skill_candidate_events_kind_check
    CHECK (kind IN ('created','revised','transition','feedback','verdict','independent_validation'));
