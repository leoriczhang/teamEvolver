"""Worker-only raw XLSX/JSONL preparation; model inputs never use the general table converter."""

import asyncio
import base64
import io
import tempfile
from pathlib import Path

from ..mining_contracts import validate_resource_uri
from .dataset import export
from .input import read_jsonl, read_xlsx, sha
from .transport import descendant, read, write


async def execute(engine, body, identifier):
    root = validate_resource_uri(body.get("output_uri"))
    if not root.startswith("viking://resources/"):
        raise ValueError("dataset output_uri must be a resource directory")
    job = {"account": engine.miner_config["account_id"]}
    format_ = body.get("format", "jsonl")
    if body.get("source_uri"):
        if format_ != "jsonl":
            raise ValueError("source_uri supports whitelist JSONL; XLSX uses content_base64")
        raw = (await read(engine, job, validate_resource_uri(body["source_uri"]), 20 * 1024 * 1024)).encode()
    else:
        raw = base64.b64decode(body.get("content_base64", ""), validate=True)
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ValueError("invalid dataset input size")
    if format_ not in {"xlsx", "jsonl"}:
        raise ValueError("format must be xlsx or jsonl")
    with tempfile.TemporaryDirectory(prefix="te-dialogue-prepare-") as temp:
        records = (read_xlsx(io.BytesIO(raw), filename=Path(body.get("filename", "input.xlsx")).name,
                             source_sha256=sha(raw), namespace=body.get("dataset_namespace", "taikang-bac"))
                   if format_ == "xlsx" else read_jsonl(io.StringIO(raw.decode("utf-8-sig"))))
        manifest = await asyncio.to_thread(export, records, temp, body.get("dialogue_options"))
        paths = [p for p in Path(temp).rglob("*") if p.is_file()]
        for path in sorted(paths):
            text = await asyncio.to_thread(path.read_bytes)
            await write(engine, job, descendant(root, path.relative_to(temp).as_posix()), text.decode("utf-8"))
        manifest_text = (Path(temp) / "dataset_manifest.json").read_bytes().decode("utf-8")
    result = {"prepared_source_uri": root + "/model_cases", "dataset_manifest_ref": root + "/dataset_manifest.json",
              "dataset_manifest_sha256": sha(manifest_text), "case_count": manifest["case_count"],
              "extract_unit_count": manifest["extract_unit_count"], "source_registered": False}
    # Source registration stays in the existing source API, preserving tenant ownership/tag checks.
    return {"ok": True, **result, "source_registration_required": True}
