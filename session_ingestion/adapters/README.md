# Tenant Data Source Adapters

Each top-level `.py` file in this directory is a bundled data source Adapter template. An Adapter connects to an upstream (Doris, Langfuse and others), filters records and converts them into standard Sessions; the platform only consumes standard Sessions. Start with `example-doris.py`, `example-langfuse.py` or `example-custom-mapping.py`. These examples are disabled until configured.

A file declares a literal `SOURCE` dictionary and a top-level `build_adapter()` returning an object with `health`, `list_session_ids`, `iter_session_id_batches`, `fetch_session`, `convert_session` and `close`. `SOURCE.supported_filters` and `required_filters` drive the console form; `SOURCE.max_sessions` is only the preview default and never limits a pull. Credentials come from environment variables, never source literals. Validation rules are in `_runtime.py:validate_content`.

Adapter source is versioned in PostgreSQL (`adapter_versions`) and bound per tenant in `adapter_bindings`, both managed by `repository.py:AdapterRepository`. Files in this directory become usable only after they are written to PostgreSQL with the service Root Key (`PUT /api/datasource/code`) and bound by a tenant administrator (`PUT /api/datasource`). Running pulls pin the revision current at acceptance; `worker.py` loads that revision and streams bounded batches on the Worker.

`_shared/` holds field mapping and builder helpers used by the agent files; `sources/` holds the Doris and Langfuse source implementations.

Shared mapping extracts structured user IDs, titles, prompts and trace IDs, with provenance. Add source-specific interpretation in your Adapter hooks. Doris table names use `DORIS_TRACES_TABLE`, `DORIS_OBSERVATIONS_TABLE` and `DORIS_MAPPING_TABLE`; their columns must match the queries in `sources/doris.py`. Direct access requires `pymysql`; HTTP access requires an explicit `DORIS_PROXY_URL` and verifies TLS by default.

Endpoints, scheduled pulls and the CLI (`teamEvolver datasource --tenant ACCOUNT status|bind|test|list|pull`) are documented in [Session Ingestion and Data Source API](../../docs/en/api/03-session-ingest.md#data-source-adapters) and [Data Source Onboarding](../../docs/en/guides/10-datasource-onboarding.md).
