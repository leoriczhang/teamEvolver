"""Connect any Volcengine MA through the official console API (plan/apply/status)."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


def load_env(path):
    """Read explicit environment files in memory; never execute shell content."""
    for line in Path(path).expanduser().read_text().splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key and not key.startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def resolve(value, *, planning=False):
    if isinstance(value, list):
        return [resolve(v, planning=planning) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$env"}:
        result = os.environ.get(value["$env"], "")
        if not result and not planning:
            raise ValueError("Required environment variable is missing: " + value["$env"])
        return result or "<" + value["$env"] + ">"
    if set(value) == {"$text"}:
        return Path(value["$text"]).expanduser().read_text()
    if set(value) == {"$json", "pointer"}:
        source = json.loads(Path(value["$json"]).expanduser().read_text())
        for part in value["pointer"].strip("/").split("/"):
            source = source[part.replace("~1", "/").replace("~0", "~")]
        return source
    return {k: resolve(v, planning=planning) for k, v in value.items()}


def persist_environment(path, updates):
    """Store a newly issued tenant Key once, in an explicitly chosen private file."""
    target = Path(path).expanduser()
    if any("\n" in str(v) or "\r" in str(v) for v in updates.values()):
        raise ValueError("Invalid environment value")
    lines = target.read_text().splitlines() if target.exists() else []
    lines = [line for line in lines if line.partition("=")[0] not in updates]
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ma-env-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines + [f"{k}={v}" for k, v in updates.items()]) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Console:
    def __init__(self, url, client=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Control plane must use HTTPS or loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Control plane URL cannot contain credentials")
        self.client = client or httpx.Client(base_url=url.rstrip("/") + "/", timeout=45, follow_redirects=False)
        self.tenant = "default"
        token = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")
        self.headers = {"Authorization": "Bearer " + token} if token else {}

    def request(self, method, path, *, optional=False, **kwargs):
        headers = {**self.headers, "X-Tenant-Id": self.tenant, **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, path.lstrip("/"), headers=headers, **kwargs)
        except httpx.HTTPError:
            raise ValueError("Control-plane request failed; credentials and response bodies are suppressed") from None
        if optional and response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise ValueError(f"Control-plane {method} {path}: HTTP {response.status_code}")
        return response.json()

    def login(self):
        if self.headers:
            return
        username = os.environ.get("TEAMEVOLVER_CONSOLE_USERNAME", "")
        password = os.environ.get("TEAMEVOLVER_CONSOLE_PASSWORD", "")
        if not username or not password:
            raise ValueError("Configure Root Key or console username/password environment variables")
        self.request("POST", "/api/auth/login", json={"username": username, "password": password})

    def apply(self, spec, config):
        self.login()
        tenant = spec["tenant"]
        tenants = self.request("GET", "/api/tenants")["tenants"]
        exists = next((t for t in tenants if t["tenant_id"] == tenant["id"]), None)
        token_env = spec["secret_env"]["tenant_token"]
        if not exists:
            output = spec.get("credential_output")
            if not output:
                raise ValueError("New tenants require credential_output to preserve the one-time Key")
            created = self.request("POST", "/api/tenants", json={
                "display_name": tenant.get("display_name", tenant["id"]), "account_id": tenant["id"],
            })
            os.environ[token_env] = created["agent_token"]
            persist_environment(output["path"], {token_env: created["agent_token"]})
            exists = created["tenant"]
        if exists["status"] != "active":
            raise ValueError("The selected tenant is disabled")
        self.tenant = tenant["id"]
        if spec.get("skills"):
            current = self.request("GET", "/api/skill-evolution/settings")
            settings = spec["skills"]
            self.request("PUT", "/api/skill-evolution/settings", json={
                "revision": current["revision"],
                "evolve": {k: v for k, v in settings.items() if k not in {"auto_evaluate", "publish_mode"}},
                "validation": {k: v for k, v in settings.items() if k in {"auto_evaluate", "publish_mode"}},
            })
        if spec.get("replay"):
            current = self.request("GET", "/api/replay/settings")
            self.request("PUT", "/api/replay/settings", json={
                "expected_revision": current["revision"], "config": spec["replay"],
            })
        if spec.get("model"):
            model = dict(spec["model"])
            key = os.environ.get(spec["secret_env"].get("model_api_key", ""), "")
            if key:
                model["api_key"] = key
            current = self.request("GET", "/api/model-settings")
            self.request("PUT", "/api/model-settings", json={**model, "revision": current["revision"]})
        ident = spec["connection_id"]
        existing = self.request("GET", "/api/ma/connections/" + quote(ident, safe=""), optional=True)
        for field, env in spec["secret_env"].items():
            if field == "model_api_key":
                continue
            secret = os.environ.get(env, "")
            if secret:
                config[field] = secret
            elif not existing:
                raise ValueError("Required credential environment variable is missing: " + env)
        if existing:
            self.request("PUT", "/api/ma/connections/" + ident,
                         json={**config, "expected_revision": existing["revision"]})
            job = self.request("POST", f"/api/ma/connections/{ident}/discover", json={})
        else:
            job = self.request("POST", "/api/ma/connections", json={**config, "connection_id": ident},
                               headers={"Idempotency-Key": "connect:" + ident})
        output = spec.get("credential_output")
        if output and output.get("values"):
            persist_environment(output["path"], output["values"])
        # Read-only model test is queued through the existing formal settings API.
        model_job = self.request("POST", "/api/model-settings/test", json={}) if spec.get("model") else None
        return {"tenant_id": tenant["id"], "connection_id": ident, "job_id": job["job_id"],
                "status_url": job["status_url"], "model_test_job_id": (model_job or {}).get("job_id"),
                "key_rotated": False}

    def status(self, spec):
        self.login()
        self.tenant = spec["tenant"]["id"]
        ident = quote(spec["connection_id"], safe="")
        connection = self.request("GET", "/api/ma/connections/" + ident)
        return {"connection": connection, "resources": self.request("GET", f"/api/ma/connections/{ident}/resources"),
                "releases": self.request("GET", f"/api/ma/connections/{ident}/releases")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "status"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--url", default=os.environ.get("TEAMEVOLVER_URL", "http://127.0.0.1:52010"))
    args = parser.parse_args()
    try:
        raw = json.loads(args.config.read_text())
        for path in [*args.env_file, *raw.get("environment_files", [])]:
            load_env(path)
        spec = resolve({k: v for k, v in raw.items() if k != "environment_files"},
                       planning=args.command == "plan")
        config = spec["connection"]
        if set(config) & {"ma_api_key", "tenant_token"}:
            raise ValueError("Credentials belong in secret_env, not connection JSON")
        if args.command == "plan":
            print(json.dumps({"tenant_id": spec["tenant"]["id"], "connection_id": spec["connection_id"],
                              "connection": config, "credential_environment_variables": spec["secret_env"],
                              "expected_resources": spec.get("expected_resources"), "paid_calls": 0},
                             ensure_ascii=False, indent=2))
            return
        console = Console(args.url)
        try:
            result = console.apply(spec, config) if args.command == "apply" else console.status(spec)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            console.client.close()
    except (ValueError, KeyError, OSError):
        raise SystemExit("MA connection failed; check configuration, credentials and console job status") from None


if __name__ == "__main__":
    main()
