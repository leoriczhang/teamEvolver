# Tenant Datasource Onboarding

Each tenant explicitly binds one top-level `.py` file in `session_ingestion/adapters/`. Files
cannot be shared between tenants and bindings are never inherited from default.
Upstream connection, project selection, filters and conversion live in this
folder. The platform consumes standard Sessions only.

Declare literal `SOURCE` metadata (`label`, `provider`, `enabled`,
`supported_filters`, `required_filters`, `max_sessions`) and `build_adapter()`.
The factory returns `health`, `list_session_ids`, `fetch_session`,
`convert_session`, `close`, and optionally `preview_sessions`.
Use thread-safe clients: up to eight Sessions are processed concurrently.

Admins can upload, create, and edit runtime copies. Unsaved source can be
validated, health-tested, previewed, or used to convert one Session without
writing to disk. Saving uses revision checks and atomic replacement.
Runtime copies can be lost on deployment or container replacement; contact the
project Owner to merge verified changes into `session_ingestion/adapters/` and release them.

The console exposes file binding, source editing, connectivity testing,
preview and pull. `GET/PUT /api/datasource` reads or binds; PUT accepts
`{"file":"product-agent.py"}` (empty string unbinds).
`GET/PUT /api/datasource/code` reads or saves a runtime copy, while
`POST /api/datasource/code/test` runs unsaved source.
`POST /api/datasource/test`, `/sessions`, `/pull` perform their named actions.
Admin console sessions or a Root Key select the Account using `X-Tenant-Id`.
Tenant tokens may only pull for their authenticated tenant, and cannot reach
admin interfaces (`/api/*`) or the Agent registration route
(`/internal/agents/register`).

Default persists `datasource.adapter` in YAML; other tenants persist
`datasource_adapter` in overrides. Missing or broken files fail closed.
Unknown filters return 400, duplicate bindings 409, concurrent pull limits 429.
Top-level files reload by content hash; shared modules and environment changes
require restart. Credentials must come from environment variables.

Deploy the complete adapters package and rebuilt console, review each upstream
project, explicitly bind existing Accounts, probe and preview a narrow window,
then pull a small batch. No production migration is automatic.
The land-network adapter currently resolves its project name via the mapping table.

Old configuration/mapper/converter endpoints return 410. Old
`/langfuse/pull` and `/langfuse/sessions` URLs enter the new tenant-file flow.
Legacy Python imports and stored fields only support offline migration.
CLI commands use `teamEvolver datasource --tenant ACCOUNT status|test|list|pull`.
Outbound platform telemetry remains independent under Global Observability.
