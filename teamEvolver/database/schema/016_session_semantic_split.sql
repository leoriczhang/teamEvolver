-- Add the semantic split checkpoint between normalization and analysis.
--
-- The split LLM runs in the Worker. Its immutable plan is recorded before
-- child ingestion fan-out so retries reuse the same topic boundaries.

ALTER TABLE {schema}.session_stage_results
    DROP CONSTRAINT IF EXISTS session_stage_results_stage_check;

ALTER TABLE {schema}.session_stage_results
    ADD CONSTRAINT session_stage_results_stage_check
    CHECK (stage IN ('normalize', 'split', 'analyze', 'trace_analyze', 'archive'));
