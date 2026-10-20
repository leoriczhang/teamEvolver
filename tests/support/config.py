"""Tenant-aware configuration repository SQL double."""

from __future__ import annotations


class FakeDB:
    """Shared store across connections; enforces app.tenant_id for tenant tables."""

    def __init__(self) -> None:
        self.global_config = {"revision": 1, "config": {}}
        self.tenant_configs: dict[tuple[str, str], dict] = {}
        self.secrets: dict[tuple[str, str], dict] = {}


class FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, db: FakeDB, schema: str = "teamevolver") -> None:
        self.db = db
        self.schema = schema
        self.tenant = ""

    def transaction(self):
        return FakeTx(self)

    def _current_tenant(self) -> str:
        return self.tenant

    async def execute(self, sql: str, *args):
        if "set_config('app.tenant_id'" in sql:
            self.tenant = str(args[0]) if args else ""
            return "SELECT 1"
        if "set_config('search_path'" in sql:
            return "SELECT 1"
        if "INSERT INTO" in sql and "tenant_configs" in sql:
            tenant_id, module, config = args
            self.db.tenant_configs.setdefault((tenant_id, module), {"revision": 1, "config": config})
            return "INSERT 0 1"
        if "INSERT INTO" in sql and "secret_refs" in sql:
            tenant_id, ref, purpose, ciphertext, fp = args
            self.db.secrets[(tenant_id, ref)] = {
                "secret_ref": ref,
                "purpose": purpose,
                "ciphertext": ciphertext,
                "fingerprint": fp,
                "created_at": "now",
            }
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, sql: str, *args):
        if "teamevolver_config" in sql:
            g = self.db.global_config
            return {"revision": g["revision"], "config": g["config"]}
        if "tenant_configs" in sql and sql.strip().startswith("SELECT"):
            module = args[0]
            row = self.db.tenant_configs.get((self._current_tenant(), module))
            if row is None:
                return None
            return {
                "tenant_id": self._current_tenant(),
                "module": module,
                "revision": row["revision"],
                "config": row["config"],
            }
        if "secret_refs" in sql and "ciphertext" in sql and "SELECT ciphertext" in sql:
            ref = args[0]
            row = self.db.secrets.get((self._current_tenant(), ref))
            return {"ciphertext": row["ciphertext"]} if row else None
        if "secret_refs" in sql and sql.strip().startswith("SELECT secret_ref"):
            ref = args[0]
            row = self.db.secrets.get((self._current_tenant(), ref))
            return dict(row) if row else None
        return None

    async def fetchval(self, sql: str, *args):
        if "UPDATE" in sql and "teamevolver_config" in sql:
            new_rev, expected = args[1], args[2]
            if self.db.global_config["revision"] != expected:
                return None
            self.db.global_config = {"revision": new_rev, "config": args[0]}
            return new_rev
        if "UPDATE" in sql and "tenant_configs" in sql:
            config, new_rev, module, expected = args
            key = (self._current_tenant(), module)
            row = self.db.tenant_configs.get(key)
            if row is None or row["revision"] != expected:
                return None
            self.db.tenant_configs[key] = {"revision": new_rev, "config": config}
            return new_rev
        return None


class FakePool:
    def __init__(self, db: FakeDB, schema: str = "teamevolver") -> None:
        self._db = db
        self._schema = schema

    @property
    def schema(self):
        return self._schema


class FakeTxManager:
    def __init__(self, db: FakeDB, schema: str = "teamevolver") -> None:
        self._db = db
        self.pool = FakePool(db, schema)

    def conn_for(self, tenant_id: str = "") -> FakeConn:
        conn = FakeConn(self._db, self.pool.schema)
        conn.tenant = tenant_id
        return conn
