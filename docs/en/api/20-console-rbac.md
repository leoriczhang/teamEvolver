# Console Identity and Tenant Authorization

## API implementation

The console uses three identity layers:

| Identity | Scope | Main capabilities |
|----------|-------|-------------------|
| Platform administrator | Platform | Tenant lifecycle, platform configuration, global audit, login-account management |
| Tenant administrator | One tenant | Members, Tenant Key, tenant configuration, team-asset publication and approval |
| Member | One tenant | Workbench, Sessions, personal assets, team reads and publication requests |

A platform administrator can list tenants but does not implicitly receive tenant governance authority. After selecting a tenant, the administrator must provide a reason and start a 30-minute support session bound to the current login Session. Platform access, support changes and authorization mutations are audited.

The browser must explicitly send `X-Tenant-Id` to enter a tenant. Every request revalidates tenant status, account status and membership. Agent identity remains independent of console accounts and memberships: Agents use the Tenant Key plus their declared `user_id`.

Code entry points:

- `teamEvolver/admin/identity_store.py:IdentityStore`
- `teamEvolver/admin/authorization.py:bind_console_user`
- `teamEvolver/proxy/identity_routes.py:register_identity_routes`

## Endpoints and parameters

All endpoints use the console HttpOnly Session Cookie. Tenant endpoints also require `X-Tenant-Id` to match the path `tenant_id`.

| Method | Path | Authorization | Purpose |
|--------|------|---------------|---------|
| GET | `/api/auth/status` | Logged-in account | Return platform identity, current tenant role, capabilities, available tenants and support state |
| GET | `/api/identity/tenants` | Logged-in account | List active tenants selectable by the account |
| POST | `/api/identity/support` | Platform administrator | Start 30-minute support mode for the current Session |
| DELETE | `/api/identity/support` | Platform administrator | Stop support mode for the current Session |
| GET | `/api/identity/audit?limit=100` | Platform administrator | Read the latest authorization events; `limit` is clamped to 1-500 |
| GET | `/api/tenants/{tenant_id}/members` | Tenant administrator or support mode | List tenant memberships |
| PUT | `/api/tenants/{tenant_id}/members/{user_id}` | Tenant administrator or support mode | Create or update a membership |
| PUT | `/api/identity/users/{user_id}/access` | Platform administrator | Update platform identity or account status |

Support request:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes | Active tenant being supported |
| `reason` | string | Yes | Audit reason, retained up to 500 characters |

Membership updates accept `role` (`tenant_admin` / `member`) and `status` (`active` / `disabled`). Platform access updates only accept `platform_role` (`platform_admin` / `user`) and `status`.

## Examples

```bash
curl -X POST 'http://localhost:52010/api/identity/support' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: tenant-a' \
  -b 'teamEvolver_console_session=<session>' \
  --data '{"tenant_id":"tenant-a","reason":"Investigate tenant configuration"}'
```

```bash
curl -X PUT 'http://localhost:52010/api/tenants/tenant-a/members/alice' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: tenant-a' \
  -b 'teamEvolver_console_session=<session>' \
  --data '{"role":"tenant_admin","status":"active"}'
```

## Responses, errors and migration

The `user` object from `GET /api/auth/status` includes `platform_role`, `tenant_role`, validated `tenant_id`, effective `capabilities`, and the Session-bound `support` record.

| Status | detail | Cause |
|--------|--------|-------|
| 403 | `PLATFORM_ADMIN_REQUIRED` | A non-platform administrator called a platform endpoint |
| 403 | `TENANT_ADMIN_REQUIRED` | A member attempted tenant governance |
| 403 | `SUPPORT_MODE_REQUIRED` | A platform administrator has no support mode for the selected tenant |
| 403 | `TENANT_ACCESS_DENIED` | The selected tenant is disabled or inaccessible |
| 403 | `TENANT_MEMBERSHIP_REQUIRED` | No tenant was explicitly selected or no membership exists |
| 409 | `LAST_PLATFORM_ADMIN` | The change would remove the final active platform administrator |
| 409 | `LAST_TENANT_ADMIN` | The change would remove the final active administrator of a tenant |

Legacy identity migration runs once:

1. A legacy `admin` becomes a `tenant_admin` in `default`; it is not automatically promoted to platform administrator.
2. Before the first upgraded start, set `TEAMEVOLVER_PLATFORM_ADMINS=<legacy-user-id>[,<legacy-user-id>...]` to explicitly promote selected users.
3. A fresh bootstrap account becomes platform administrator. Self-registered accounts receive no implicit `default` membership.
4. PostgreSQL deployments use dedicated control tables. Standalone deployments use `console_identity.sqlite3`. Legacy JSON is not authoritative after migration.
5. After migration, do not edit legacy JSON to retry. Use an existing platform administrator to change access.

If migration leaves no platform administrator, an operator can use the configured
`TEAMEVOLVER_ROOT_API_KEY` with `PUT /api/identity/users/{user_id}/access` to recover one. Never expose the Root Key to browser code.
