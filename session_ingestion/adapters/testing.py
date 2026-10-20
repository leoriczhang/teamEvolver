"""Worker-only adapter health/preview execution; validation never executes code."""
from ._runtime import load_from_content


def execute(source, filename, request):
    adapter, metadata, _ = load_from_content(source, filename)
    mode = request["operation"]
    try:
        result = {"ok": True, "metadata": {"file": filename, **metadata}}
        if mode == "health":
            result["health"] = adapter.health()
        elif mode == "preview":
            ids = adapter.list_session_ids(request["filters"], max_sessions=request["max_sessions"])
            result.update(sessions=[{"session_id": sid} for sid in ids], count=len(ids))
        elif mode == "session":
            raw, traces = adapter.fetch_session(request["session_id"])
            result["session"] = adapter.convert_session(raw, traces)
        else:
            raise ValueError("unknown adapter operation")
        return result
    finally:
        adapter.close()
