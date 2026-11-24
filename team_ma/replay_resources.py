"""Reuse source references; native Sessions provide independent sandboxes."""

import copy


def effective_replay_config(config, resources):
    """Freeze resource references into one accepted Replay task."""
    value = copy.deepcopy(config)
    if resources:
        value["replay_environment_id"] = resources.get("environment_id", "")
        value["replay_vault_ids"] = list(resources.get("vault_ids") or [])
        value["replay_resources"] = copy.deepcopy(resources)
    return value


async def prepare_replay_resources(task, services, client, connection, environment):
    config = connection["config"]
    if not config.get("replay_enabled"):
        return None, {"status": "disabled"}
    if environment.get("config", {}).get("type") != "cloud":
        return None, {"status": "unsupported", "reason": "需要云端业务环境"}
    # Environment IDs identify configuration, not a running sandbox. Vault
    # references work in each Session without reading or copying secret values.
    return copy.deepcopy(environment), {
        "status": "ready", "environment_id": environment["id"],
        "vault_ids": list(config.get("vault_ids") or []),
    }
