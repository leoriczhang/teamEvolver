"""Prepare connection-owned Replay infrastructure without changing its revision."""

import copy
import re
from urllib.parse import urlsplit

from .effects import Effects
from .schemas import digest


def effective_replay_config(config, resources):
    """Freeze discovered IDs and readiness into one accepted Replay task."""
    value = copy.deepcopy(config)
    if resources:
        value["replay_environment_id"] = resources.get("environment_id", "")
        value["replay_vault_ids"] = list(resources.get("vault_ids") or [])
        value["replay_resources"] = copy.deepcopy(resources)
        if resources.get("vaults_automatic"):
            value["replay_isolation"] = {**(value.get("replay_isolation") or {}), "vaults": True}
    return value


def _receipt(value):
    if not value.get("id"):
        raise ValueError("MA Replay resource is missing an ID")
    return {"id": value["id"]}


def _environment_hash(config):
    value = {k: v for k, v in config.items() if k != "tos"}
    for key in ("networking", "env", "packages"):
        value[key] = value.get(key) or {}
    value["setup_script"] = value.get("setup_script") or ""
    return digest(value)


def _credential_key(auth):
    name = auth.get("secret_name")
    if name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    # Non-environment credentials have no secret_name. Keep their public host
    # policy in the identity so a manually configured Vault can be checked too.
    networking = auth.get("networking") or {}
    hosts = ", ".join(sorted(networking.get("allowed_hosts") or []))
    return f"{auth.get('type', '未知')}凭证" + (f"（{hosts}）" if hosts else "")


async def _owned(client, path, metadata, *, config=None):
    matches = [row for row in await client.listing(path)
               if all((row.get("metadata") or {}).get(k) == v for k, v in metadata.items())]
    if len(matches) > 1:
        raise ValueError("Multiple owned Replay resources require reconciliation")
    if not matches:
        return None
    value = await client.request("GET", f"{path}/{matches[0]['id']}")
    if config is not None and _environment_hash(value.get("config") or {}) != _environment_hash(config):
        raise ValueError("Owned Replay Environment configuration changed")
    return _receipt(value)


async def prepare_replay_resources(task, services, client, connection, environment):
    config = connection["config"]
    if not config.get("replay_enabled"):
        return None, {"status": "disabled"}
    if environment.get("config", {}).get("type") != "cloud":
        return None, {"status": "unsupported", "reason": "需要云端业务环境"}
    effects = Effects(services, task.tenant_id, task.subject_id, "replay-infrastructure", task=task)
    scope = digest([task.tenant_id, config["ma_base_url"], task.subject_id])
    owner = {"te_owner": scope, "te_purpose": "replay-infrastructure"}
    automatic_env = not config.get("replay_environment_id")
    if automatic_env:
        # Retain runtime and dependencies; use MA's default per-Session output storage.
        env_config = {k: copy.deepcopy(v) for k, v in environment["config"].items() if k != "tos"}
        env_config.setdefault("networking", {"type": "unrestricted"})
        version = _environment_hash(env_config)
        metadata = {**owner, "te_config": version}
        intent = {"name": "te-replay-" + digest([scope, version])[:40],
                  "config": env_config, "metadata": metadata}

        async def reconcile_env(row):
            return await _owned(client, "environments", metadata, config=env_config)

        async def create_env():
            found = await reconcile_env(None)
            return found or _receipt(await client.request("POST", "environments", json=intent))

        receipt = await effects.apply("ma-replay-env:" + digest([scope, version])[:40],
                                      "replay-infrastructure-environment", intent, create_env, reconcile_env)
        replay_environment = await client.request("GET", f"environments/{receipt['id']}")
        if (_environment_hash(replay_environment.get("config") or {}) != version
                or replay_environment.get("config", {}).get("tos")
                or any((replay_environment.get("metadata") or {}).get(k) != v for k, v in metadata.items())):
            raise ValueError("Owned Replay Environment readback mismatch")
    else:
        replay_environment = await client.request("GET", f"environments/{config['replay_environment_id']}")
    if replay_environment.get("id") == environment.get("id"):
        raise ValueError("Replay must use a separate Environment")

    automatic_vaults = not config.get("replay_vault_ids")
    vault_ids = list(config.get("replay_vault_ids") or [])
    required, sources = set(), {}
    for ident in config.get("vault_ids") or []:
        for credential in await client.listing(f"vaults/{ident}/credentials"):
            auth = credential.get("auth") or {}
            name = _credential_key(auth)
            required.add(name)
            sources.setdefault(name, auth)
    binding = config.get("runtime_binding") or {}
    if binding.get("source") == "openviking":
        required.add("OPENVIKING_API_KEY")
    if automatic_vaults:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            secret = await services.extras["secret_repo"].describe(conn, secret_ref=config["ma_secret_ref"])
        if not secret:
            raise ValueError("Replay credential reference is unavailable")
        metadata = {**owner, "te_config": digest([config.get("vault_ids"), secret["fingerprint"]])}
        intent = {"display_name": "TE Replay " + scope[:16], "metadata": metadata}

        async def reconcile_vault(row):
            return await _owned(client, "vaults", metadata)

        async def create_vault():
            found = await reconcile_vault(None)
            return found or _receipt(await client.request("POST", "vaults", json=intent))

        receipt = await effects.apply("ma-replay-vault:" + digest([scope, metadata])[:40],
                                      "replay-infrastructure-vault", intent, create_vault, reconcile_vault)
        vault_ids = [receipt["id"]]
        vault = await client.request("GET", f"vaults/{vault_ids[0]}")
        if any((vault.get("metadata") or {}).get(k) != v for k, v in metadata.items()):
            raise ValueError("Owned Replay Vault readback mismatch")
        if set(vault_ids) & set(config.get("vault_ids") or []):
            raise ValueError("Replay must use separate Vaults")
        # MA credentials are write-only. Only this existing, tenant-owned reference
        # has known semantics; never substitute a global Key for another service.
        host = urlsplit(config["ma_base_url"]).hostname
        ark = sources.get("ARK_API_KEY") or {}
        networking = ark.get("networking") or {}
        hosts = networking.get("allowed_hosts") or []
        if not ark or networking.get("type") == "unrestricted" or host in hosts:
            await _seed_ark(task, services, client, effects, scope, vault_ids[0], config["ma_secret_ref"], host)
            required.add("ARK_API_KEY")
    elif set(vault_ids) & set(config.get("vault_ids") or []):
        raise ValueError("Replay must use separate Vaults")
    available = set()
    for ident in vault_ids:
        for credential in await client.listing(f"vaults/{ident}/credentials"):
            available.add(_credential_key(credential.get("auth") or {}))
    missing = sorted(required - available)
    resources = {"status": "credentials_missing" if missing else "ready",
                 "environment_id": replay_environment["id"], "vault_ids": vault_ids,
                 "environment_automatic": automatic_env, "vaults_automatic": automatic_vaults,
                 "missing_credentials": missing}
    return replay_environment, resources


async def _seed_ark(task, services, client, effects, scope, vault_id, secret_ref, host):
    secrets = services.extras["secret_repo"]
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        described = await secrets.describe(conn, secret_ref=secret_ref)
    if not described:
        raise ValueError("Replay credential reference is unavailable")
    auth = {"type": "environment_variable", "secret_name": "ARK_API_KEY",
            "networking": {"type": "limited", "allowed_hosts": [host]}}
    intent = {"vault_id": vault_id, "secret_ref": secret_ref, "fingerprint": described["fingerprint"],
              "display_name": "TE Replay Ark", "auth": auth}

    async def reconcile(row):
        matches = [c for c in await client.listing(f"vaults/{vault_id}/credentials")
                   if c.get("auth") == auth]
        return _receipt(matches[0]) if len(matches) == 1 else None

    async def send():
        found = await reconcile(None)
        if found:
            return found
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            plaintext = await secrets.resolve(conn, secret_ref=secret_ref)
        value = await client.request("POST", f"vaults/{vault_id}/credentials",
                                     json={"display_name": intent["display_name"],
                                           "auth": {**auth, "secret_value": plaintext}})
        return _receipt(value)

    await effects.apply("ma-replay-credential:" + digest([scope, intent])[:40],
                        "replay-infrastructure-credential", intent, send, reconcile)
