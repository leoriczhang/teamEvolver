-- Console identity tables are platform control-plane tables, outside tenant RLS.
-- Membership authorization is explicit and always includes tenant_id.
CREATE TABLE {schema}.console_users (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE {schema}.tenant_memberships (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, record TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE {schema}.console_support_sessions (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE {schema}.console_login_sessions (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE {schema}.console_auth_audit (
    id TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL, record TEXT NOT NULL
);
CREATE TABLE {schema}.console_auth_meta (id TEXT PRIMARY KEY, record TEXT NOT NULL);
