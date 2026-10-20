# Console Identity and Tenant Permissions API

The console uses three levels of identity:

| Identity | Scope | Capabilities |
| --- | --- | --- |
| Platform administrator (`platform_role=platform_admin`) | Whole platform | Create and disable tenants, platform settings, account management, permission audit; can enter any active tenant with tenant administrator capabilities |
| Tenant administrator (member `role=tenant_admin`) | One tenant | Members, tenant key, tenant configuration, team asset publishing and approval |
| Member (member `role=member`) | One tenant | Workbench, personal assets, team asset reads and publish requests |

The browser must send an explicit `X-Tenant-Id` on every request, and the server re-checks tenant status, account status and membership with `teamEvolver/admin/authorization.py:resolve_console_membership`. Capability sets are in [API Overview · Console Session](./01-overview.md#console-session). Agents do not use console accounts; see [Protocol v2](../agent-integrations/06-protocol-v2.md).

Accounts, memberships and audit are stored in PostgreSQL and accessed through `teamEvolver/admin/identity_store.py:IdentityStore`.

Code entry points:

- Authentication: `teamEvolver/admin/authn.py:build_console_authorizer`
- Login, users, identity, members: `teamEvolver/admin/api/__init__.py:create_router`
- Tenants: `teamEvolver/tenants/api/__init__.py:create_router`
- Platform and tenant settings: `teamEvolver/admin/api/settings.py:create_router`

## Login

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/auth/status` | Anonymous | Login state, current user, available tenants, `needs_setup`, `self_registration_enabled`, `customer_mode` |
| POST | `/api/auth/bootstrap` | Anonymous (only when no account exists) | Creates the first account as platform administrator; password at least 12 characters |
| POST | `/api/auth/login` | Anonymous | `{"username", "password"}`, sets the Session cookie |
| POST | `/api/auth/register` | See below | Self-registration of a regular account |
| POST | `/api/auth/logout` | Logged in | Revokes the Session and clears the cookie |

`user` in `/api/auth/status` contains `platform_role`, `tenant_id` (the validated current tenant, otherwise null), `tenant_role` and `capabilities`.

### Self-Registration

`POST /api/auth/register` takes `username`, `password` and optional `display_name`, `email`. The created account is always a regular user with no tenant membership.

- Anonymous registration is allowed when the bootstrap setting `self_registration_enabled=true` (environment variable `TEAMEVOLVER_SELF_REGISTRATION_ENABLED`).
- When off, only the service Root Key can call it; other requests return `403 SELF_REGISTRATION_DISABLED`.
- Before the first account is initialized it returns 409; an existing username returns 409.

```bash
curl -X POST http://localhost:52010/api/auth/register \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username": "new-user", "password": "replace-with-a-strong-password"}'
```

## Accounts

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/users` | Logged in | Platform administrators see all accounts; tenant administrators see active members of their tenant; members see only themselves |
| GET | `/api/users/{user_id}` | Self or platform administrator | Account detail |
| POST | `/api/users` | Platform administrator | Create a regular account; `role` or `platform_role` cannot be set |
| PUT | `/api/users/{user_id}/profile` | Self or platform administrator | Update profile |
| DELETE | `/api/users/{user_id}` | Platform administrator | Delete account |
| PUT | `/api/identity/users/{user_id}/access` | Platform administrator | `{"platform_role": "platform_admin"|"user", "status": "active"|"disabled"}` |
| GET | `/api/identity/tenants` | Logged in | Active tenants the account can enter |
| GET | `/api/identity/audit?limit=100` | Platform administrator | Recent permission audit, `limit` 1–500 |

Removing the last active platform administrator returns `409 LAST_PLATFORM_ADMIN`. When no platform administrator is available, recover by calling `PUT /api/identity/users/{user_id}/access` with the Root Key.

## Tenants and Members

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/tenants` | Logged in | Platform administrators see all tenants, others see their own; includes `machine_credential` (whether the `default` tenant key is configured) |
| POST | `/api/tenants` | Platform administrator | `{"display_name", "account_id"?}` creates a tenant |
| POST | `/api/tenants/{tenant_id}/status` | Platform administrator | `{"status": "active"|"disabled"}` |
| POST | `/api/tenants/{tenant_id}/rotate-token` | That tenant's administrator | Rotates the tenant key and returns the new `agent_token`; the `default` tenant cannot be rotated |
| GET | `/api/tenants/{tenant_id}/config` | That tenant's administrator | Tenant configuration overrides (credential fields only report presence) and editable keys |
| PUT | `/api/tenants/{tenant_id}/config` | That tenant's administrator | `{"overrides": {...}}` merges; a `null` value deletes the key |
| GET | `/api/tenants/{tenant_id}/members` | That tenant's administrator | Member list |
| PUT | `/api/tenants/{tenant_id}/members/{user_id}` | That tenant's administrator | `{"role": "tenant_admin"|"member", "status": "active"|"disabled"}` adds or updates a member |

The `tenant_id` in the path must equal the current `X-Tenant-Id`, otherwise `403 TENANT_MISMATCH`.

When a tenant is created:

1. The tenant row is written to PostgreSQL together with default configuration rows for the 5 modules (`session`, `skills`, `replay`, `memory`, `miner`).
2. A same-named OpenViking Account is created on a best-effort basis and written to `sharing_viking_account`; on failure the tenant is not rolled back and `openviking_account.error` in the response gives the reason.
3. The OpenViking directory skeleton is initialized on a best-effort basis, with the outcome returned in `openviking_dirs`.
4. The plaintext `agent_token` is returned once; the server stores only its SHA-256.

Tenant configuration overrides use the flat field names of `TeamEvolverConfig`. Tenant administrators can only write model fields (`llm_provider`, `llm_api_base`, `llm_model_id`, `llm_api_key`, `llm_max_tokens`, `llm_temperature`, `llm_max_concurrency`, `llm_queue_capacity`, `llm_request_timeout_seconds`) and quotas (`max_concurrent_sessions`, `max_evolve_per_day`); other fields return `403 PLATFORM_CONFIG_REQUIRED`. Platform administrators can write every field except service-level ones, and unknown fields return 400. Module configuration (`tenant_configs`) is maintained by each module's endpoints, not this one.

## Platform and Tenant Settings

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET / PUT | `/api/model-settings` | Tenant administrator | Tenant evolution model: `model`, `base_url`, `temperature`, `max_tokens`, `api_key` / `clear_api_key`, stored as `model` in the `skills` module configuration |
| POST | `/api/model-settings/test` | Tenant administrator | Test model connectivity, returns 202 |
| GET | `/api/sharing-config` | Tenant administrator | OpenViking deployment, address, Account, team User, whether a key exists |
| PUT | `/api/sharing-config` | Platform administrator | Change the fields above; `api_key` is stored as a Secret Ref in the global configuration |
| POST | `/api/sharing-config/bootstrap-dirs` | Platform administrator | `{"account"?}` initializes the OpenViking directory skeleton, returns 202 |
| GET / PUT | `/api/team-settings` | Platform administrator | Console display name `display_name` |
| GET / PUT | `/api/langfuse-tracing-config` | Platform administrator | Outbound Langfuse tracing configuration |
| POST | `/api/langfuse-tracing-config/test` | Platform administrator | Test tracing, returns 202 |
| GET | `/api/storage/status` | Tenant administrator | PostgreSQL pool status |
| GET | `/api/openviking-accounts` | Platform administrator | OpenViking Account list |
| GET | `/api/openviking-accounts/{account}/users` | Platform administrator | Users in the Account |
| POST | `/api/openviking-accounts/{account}/import-users` | Tenant administrator | `{"user_ids": [...]}` imports them as console accounts |

Global settings are stored in `teamevolver_config` and tenant settings in `tenant_configs`, with revision optimistic locking. Credentials are encrypted as Secret Refs and need `TEAMEVOLVER_SECRET_MASTER_KEY` (falling back to `TEAMEVOLVER_ROOT_API_KEY` as master key when unset); when both are missing the response is 503.

## Personal Skills and Publish Requests

Personal Skills are stored in the OpenViking personal space (`personal_skills` scope).

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/users/{user_id}/skills?space=personal` | Self or tenant administrator | List Skills in the personal space or the OpenViking team directory (`space=team`) |
| GET / DELETE | `/api/users/{user_id}/skills/{name}` | Self or tenant administrator | Read / delete |
| POST | `/api/users/{user_id}/skills` | Self or tenant administrator | Save: `space`, `name`, `skill_md` or `description` / `category` / `body` |
| POST | `/api/users/{user_id}/share` | Self or tenant administrator | `{"direction": "personal_to_team"|"team_to_personal", "skill_names": [...]}` copies; `personal_to_team` is tenant administrators only |
| POST | `/api/users/{user_id}/publish-requests` | Self or tenant administrator | `{"skill_names", "note"}` requests publishing to the team |
| GET | `/api/skill-publish-requests` | Logged in | Tenant administrators see all, members see their own requests |
| POST | `/api/skill-publish-requests/{request_id}/approve` | Tenant administrator | Approve, copying the personal Skill into the OpenViking team directory |
| POST | `/api/skill-publish-requests/{request_id}/reject` | Tenant administrator | Reject |

Approvals and shares write to the OpenViking team Skill directory (`team_skills` scope) for Context retrieval. The team Skills Agents pull through `/sync/skills` come from the PostgreSQL [Team Skill Library](./09-skills-admin.md); the two are maintained separately.

## Errors

| Status | detail | Cause |
| --- | --- | --- |
| 401 | `login required` | Not logged in |
| 401 | `invalid username or password` | Login failed |
| 403 | `USER_DISABLED` | Account disabled |
| 403 | `PLATFORM_ADMIN_REQUIRED` | Platform endpoint called by a non-platform administrator |
| 403 | `TENANT_ADMIN_REQUIRED` | Tenant governance by a non-tenant administrator |
| 403 | `TENANT_ACCESS_DENIED` | Selected a tenant without access or a disabled tenant |
| 403 | `TENANT_MEMBERSHIP_REQUIRED` | No tenant selected or no membership |
| 403 | `TENANT_MISMATCH` | Path tenant differs from `X-Tenant-Id` |
| 403 | `PLATFORM_CONFIG_REQUIRED` | A tenant administrator wrote a platform field |
| 409 | `LAST_PLATFORM_ADMIN` / `LAST_TENANT_ADMIN` | The operation would remove the last active platform or tenant administrator |
| 409 | `users already exist` | bootstrap called again after initialization |
