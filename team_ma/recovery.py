"""Recover only uniquely identified resources from recorded owned create intents."""

import hashlib

from team_skills.library.bundle import bundle_tree_sha256
from teamEvolver.managed_agents.events import tag_values

from .collection import download_url
from .discovery import read_skill_zip


async def recover_owned(client, effect):
    intent, kind = effect["intent"], effect["kind"]
    if kind == "replay-skill":
        rows = await client.listing("skills", source="custom")
        matches = [r for r in rows if r.get("display_title") == intent["name"]]
        if len(matches) == 1:
            row = matches[0]
            version = str(row.get("latest_version") or row.get("version") or "1")
            data = await client.download(f"skills/{row['id']}/versions/{version}/content")
            if bundle_tree_sha256(read_skill_zip(data).files) == intent["tree_sha256"]:
                return {**row, "version": version}
    elif kind == "replay-agent":
        rows = await client.listing("agents")
        matches = [r for r in rows if r.get("name") == intent["name"]]
        if len(matches) == 1:
            row = await client.request("GET", f"agents/{matches[0]['id']}")
            if all(row.get(k) == v for k, v in intent.items()):
                return row
    elif kind == "replay-session":
        rows = await client.listing("sessions", agent_id=intent["agent"]["id"])
        matches = [r for r in rows if set(tag_values(intent["tags"])) <= set(tag_values(r.get("tags")))]
        if len(matches) == 1:
            return await client.request("GET", f"sessions/{matches[0]['id']}")
    elif kind == "replay-file" and intent.get("filename"):
        rows = await client.listing("files", purpose="agent")
        matches = [r for r in rows if (r.get("filename") or r.get("name")) == intent["filename"]]
        if len(matches) == 1:
            row = await client.request("GET", f"files/{matches[0]['id']}")
            if row.get("download_url"):
                data = await download_url(client, row["download_url"], 20 * 1024 * 1024)
                if hashlib.sha256(data).hexdigest() == intent["sha256"]:
                    return row
    return None


async def delete_owned(client, kind, remote):
    from teamEvolver.managed_agents import MAError

    path = {"replay-session": "sessions", "replay-agent": "agents",
            "replay-skill": "skills", "replay-file": "files"}.get(kind)
    if path is None:
        return False
    try:
        if path == "sessions":
            current = await client.request("GET", f"sessions/{remote}")
            if current.get("status") not in {"idle", "terminated"}:
                await client.request("POST", f"sessions/{remote}/events",
                                     json={"events": [{"type": "user.interrupt"}]})
                return False
        if path == "skills":
            versions = await client.listing(f"skills/{remote}/versions")
            for version in sorted(versions, key=lambda v: int(v["version"])):
                await client.request("DELETE", f"skills/{remote}/versions/{version['version']}")
        await client.request("DELETE", f"{path}/{remote}")
        return True
    except MAError as exc:
        return exc.status == 404
