"""Read Memory Change snapshots through the Memory OpenViking adapter."""
from .openviking import clients, request


async def read_change(services, tenant, change):
    from .repository import MemoryRepository
    async with services.tx.tenant_tx(tenant) as conn:
        row = await MemoryRepository(services.tx).get(conn, change["run_id"])
    client, _ = await clients(services, row["input"])
    try:
        async def read(uri):
            if not uri:
                return ""
            tree = await request(client, "GET", "/api/v1/fs/tree",
                                 params={"uri": uri, "node_limit": 1000, "level_limit": 20, "output": "original"})
            entries = tree.get("entries", tree.get("items", [])) if isinstance(tree, dict) else tree
            if not isinstance(entries, list) or len(entries) >= 1000:
                raise ValueError("Memory snapshot exceeds read budget")
            parts = []
            total = 0
            for entry in entries:
                if entry.get("isDir", entry.get("is_dir")):
                    continue
                text = await request(client, "GET", "/api/v1/content/read", params={"uri": entry["uri"]})
                if not isinstance(text, str):
                    raise ValueError("Memory snapshot must be text")
                total += len(text.encode())
                if total > 2 * 1024 * 1024:
                    raise ValueError("Memory snapshot exceeds 2 MiB")
                parts.append(text)
            return "\n\n".join(parts)
        return await read(change["before_ref"]), await read(change["after_ref"])
    finally:
        await client.aclose()
