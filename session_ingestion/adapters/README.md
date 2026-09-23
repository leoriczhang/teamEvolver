# Tenant Data Adapters

Each top-level `.py` file is bound to one tenant through `/api/datasource`.
Upstream connection, filtering and conversion belong here. The platform only
consumes standard Sessions. See `product-agent.py` for a complete example.

Files declare a literal `SOURCE` dictionary and `build_adapter()` returning
`health`, `list_session_ids`, `fetch_session`, `convert_session` and `close`.
`SOURCE.supported_filters` and `required_filters` drive the console form.
Credentials come from environment variables, never source literals.

Non-default tenants persist `datasource_adapter` in tenant overrides. Default
uses `datasource.adapter` in the active YAML. Bindings are explicit and never
inherited or guessed from display names. One file cannot bind to two tenants.
Missing or broken files fail closed. Top-level file changes reload by content
hash; shared module/environment changes require restart.

Admins can upload, create and edit runtime copies. Unsaved drafts can be
validated, health-tested, previewed or used to convert one Session without
writing to disk. Saving uses an expected revision and atomic replacement.
Runtime copies are not durable across deployment or container replacement;
contact the project Owner to merge verified changes into
`session_ingestion/adapters/` and release.

The console also supports binding, saved connection testing, Session preview
and ingestion. `/api/datasource` exposes GET/PUT; `/code` is GET/PUT;
`/code/test`, `/test`, `/sessions` and `/pull` are POST. Tenant tokens only access their own
`/pull`; other operations require admin. Requests cannot select an agent or
adapter. At most four tenants pull concurrently, one pull per tenant.

Deploy the complete `session_ingestion/` package and rebuilt console. Configure
Doris environment variables, verify each file's upstream project, explicitly
bind existing Account IDs, then probe and preview before pulling a small batch.
No automatic production binding or data migration is performed.
The land-network file currently resolves its project name via the mapping table.

Old upstream configuration and mapper/converter endpoints return 410.
`/langfuse/pull` and `/langfuse/sessions` alias the new tenant-file flow.
Legacy Python imports and persisted fields remain for offline migration only;
they do not drive live pulls.

CLI: `teamEvolver datasource --tenant ACCOUNT status`, `bind FILE.py`, `test`,
`list` or `pull --from-timestamp ... --to-timestamp ... --max-sessions 10`.

Outbound telemetry stays in `teamEvolver/observability/` and has its own page.
