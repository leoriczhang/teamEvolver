-- Support tenant-scoped polling of archived members and their worker tasks.
CREATE INDEX sessions_ingestion_idx ON {schema}.sessions(tenant_id,ingestion_id);
CREATE INDEX worker_tasks_ingestion_subject_idx ON {schema}.worker_tasks(tenant_id,subject_id)
    WHERE subject_type='ingestion';
