"""Immutable deferred writes and bounded exact reads through the platform client."""

import asyncio

from starlette.exceptions import HTTPException

from ..content_io import _content_text
from ..mining_contracts import validate_descendant, validate_resource_uri
from .input import canonical, sha

MAX_FILE = 2 * 1024 * 1024


async def read(engine, job, uri, maximum=MAX_FILE):
    uri = validate_resource_uri(uri)
    result = await engine._knowledge_request(
        job["account"], "GET", "/api/v1/content/read", timeout=30,
        params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"})
    text = _content_text(result)
    if len(text.encode("utf-8")) > maximum:
        raise ValueError("dialogue_file_exceeds_limit")
    return text


def descendant(root, path):
    if not isinstance(path, str) or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
        raise ValueError("unsafe_dataset_path")
    return validate_descendant(root, root.rstrip("/") + "/" + path, allow_root=False)


async def write(engine, job, uri, text):
    """Read-before-create plus mode=create prevents overwrites on replay."""
    try:
        previous = await read(engine, job, uri)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        if previous != text:
            raise ValueError("immutable_dialogue_artifact_changed")
        return
    parent = uri.rsplit("/", 1)[0]
    roots = []
    while parent.startswith("viking://resources/"):
        roots.append(parent)
        parent = parent.rsplit("/", 1)[0]
    client = engine._round_client(job)
    for directory in reversed(roots):
        await client.mkdir(directory)
    body = {"root_uri": roots[0], "wait": True, "defer_processing": True,
            "operations": [{"uri": uri, "content": text, "mode": "create"}]}
    result = await engine._knowledge_request(
        job["account"], "POST", "/api/v1/content/batch-write", timeout=180,
        extra_headers={"Idempotency-Key": "te-dd-write-" + sha(canonical(body))}, json=body)
    if result.get("conflicts"):
        if await read(engine, job, uri) != text:
            raise ValueError("immutable_dialogue_write_conflict")
    if result.get("semantic_status") != "deferred" or result.get("vector_status") != "deferred":
        raise ValueError("deferred_dialogue_write_not_acknowledged")


async def artifact(engine, job, relative, text):
    await write(engine, job, job["dialogue_output_root"] + "/" + relative, text)
    await asyncio.to_thread(engine.repository.write_output, job["job_id"],
                            "semantic_reports/dialogue_dimensions/" + relative, text)
    # Artifact checkpoint precedes journal advancement, so a crash never loses a projected leaf.
    await asyncio.to_thread(engine._checkpoint, job)
