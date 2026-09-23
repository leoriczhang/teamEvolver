"""Review/import/export endpoints: drafts cannot bypass OV publication."""

import asyncio
import base64
import copy
import hashlib
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .engine.corpus.loader import SUPPORTED_SUFFIXES, extract_text
from .engine.provenance import AnnotatedPack
from .engine.review.draft import apply_decision
from .engine.validation.hugagentos_parity import DomainPackValidator
from .wire import Source


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str
    path: str
    decision: Literal["approve", "reject", "edit"]
    edited_value: dict | None = None


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(max_length=200)
    content_base64: str = Field(max_length=8_000_000)
    source_id: str = Field(min_length=1, max_length=200)
    readers: list[str] = Field(max_length=1000)


def parse_document(body):
    suffix = Path(body.filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(422, "UNSUPPORTED_DOCUMENT")
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(422, "INVALID_BASE64") from exc
    if len(raw) > 5_000_000:
        raise HTTPException(413, "DOCUMENT_TOO_LARGE")
    # Never use client-supplied directory components or extract archives to disk.
    with tempfile.TemporaryDirectory(prefix="te-ontology-document-") as folder:
        path = Path(folder) / ("document" + suffix)
        path.write_bytes(raw)
        if suffix in {".docx", ".xlsx"}:
            import zipfile

            with zipfile.ZipFile(path) as archive:
                if sum(z.file_size for z in archive.infolist()) > 30_000_000:
                    raise HTTPException(413, "DOCUMENT_EXPANSION_LIMIT")
        text = extract_text(path)
    if len(text.encode()) > 2 * 1024 * 1024:
        raise HTTPException(413, "SPLIT_SOURCE_REQUIRED")
    return Source(source_id=body.source_id, revision=hashlib.sha256(raw).hexdigest(), text=text, readers=body.readers)


def annotated_from(candidate):
    for proposal in candidate.get("schema_proposals", []):
        if proposal.get("kind") == "domain_pack":
            return proposal, AnnotatedPack.model_validate(proposal["annotated"])
    raise HTTPException(404, "DOMAIN_PACK_NOT_GENERATED")


class LegacyImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_key: str = Field(min_length=1, max_length=200)
    annotated: AnnotatedPack
    historical_versions: list[dict] = Field(default_factory=list, max_length=100)
    decisions: list[dict] = Field(default_factory=list, max_length=10000)


def install_review(api, ops, dep):
    @api.post("/imports", status_code=201)
    async def legacy(body: LegacyImport, p=dep):
        from .api import public_job

        payload = body.model_dump(mode="json", by_alias=True)
        result = {
            "candidate": {"schema_proposals": [{"kind": "domain_pack", "annotated": payload["annotated"]}]},
            "historical_versions": body.historical_versions,
            "decisions": body.decisions,
            "requires_enrichment": True,
            "publication_status": "historical_import_only",
        }
        return public_job(await ops.store.submit(p, body.import_key, payload, imported=result))

    @api.post("/documents", status_code=201)
    async def document(body: Document, p=dep):
        source = await asyncio.to_thread(parse_document, body)
        return await ops.ov(p, "POST", "/sources", source.model_dump(mode="json"))

    @api.get("/jobs/{job_id}/graph")
    async def graph(job_id: str, p=dep):
        job = await ops.store.get(p, job_id)
        candidate = job["result"].get("candidate", {})
        return {
            "entities": candidate.get("entities", []),
            "assertions": candidate.get("assertions", []),
            "domain_packs": candidate.get("schema_proposals", []),
        }

    @api.post("/jobs/{job_id}/review")
    async def decision(job_id: str, body: Decision, p=dep):
        from .api import public_job

        job = await ops.store.get(p, job_id)
        candidate = copy.deepcopy(job["result"]["candidate"])
        if body.path.startswith("assertions."):
            try:
                index = int(body.path.split(".")[1])
                item = candidate["assertions"][index]
                if index < 0:
                    raise IndexError()
            except (ValueError, IndexError):
                raise HTTPException(422, "INVALID_REVIEW_PATH") from None
            if body.decision == "reject":
                candidate["assertions"].pop(index)
            elif body.decision == "edit":
                if body.edited_value is None:
                    raise HTTPException(422, "EDIT_VALUE_REQUIRED")
                candidate["assertions"][index] = body.edited_value
            candidate.setdefault("extraction", {}).setdefault("reviews", {})[item["assertion_id"]] = {
                "decision": body.decision,
                "reviewer": p["subject"],
            }
        else:
            proposal, annotated = annotated_from(candidate)
            try:
                apply_decision(annotated, body.path, body.decision, edited_value=body.edited_value)
            except (KeyError, ValueError):
                raise HTTPException(422, "INVALID_REVIEW_DECISION") from None
            _, report = DomainPackValidator().validate(annotated.clean_pack_dict())
            if not report.valid:
                raise HTTPException(422, "DOMAIN_PACK_INVALID")
            proposal["annotated"] = annotated.model_dump(mode="json", by_alias=True)
        result = await ops.edit(p, job_id, candidate, body.expected_digest)
        await ops.store.audit(
            p,
            "candidate.reviewed",
            {
                "job_id": job_id,
                **body.model_dump(mode="json"),
                "candidate_digest": result["result"]["artifact"]["candidate_digest"],
            },
        )
        return public_job(result)

    @api.get("/jobs/{job_id}/export")
    async def export(job_id: str, p=dep):
        job = await ops.store.get(p, job_id)
        _, annotated = annotated_from(job["result"]["candidate"])
        if any(m.status not in {"approved", "rejected"} for m in annotated.meta.values()):
            raise HTTPException(409, "UNREVIEWED_DOMAIN_PACK")
        payload = annotated.clean_pack_dict()
        for kind in ["concepts", "relations", "constraints", "workflows"]:
            payload[kind] = [
                v
                for i, v in enumerate(payload.get(kind, []))
                if getattr(annotated.meta.get(f"{kind}.{i}"), "status", None) != "rejected"
            ]
        _, report = DomainPackValidator().validate(payload)
        if not report.valid:
            raise HTTPException(422, "REJECTED_ELEMENT_STILL_REFERENCED")
        await ops.store.audit(p, "domain_pack.exported", {"job_id": job_id})
        return {
            "kind": "reviewed_export_not_publication",
            "domain_pack": payload,
            "provenance": annotated.provenance_rows(),
        }

    @api.get("/jobs/{job_id}/compare/{other_id}")
    async def compare(job_id: str, other_id: str, p=dep):
        first = (await ops.store.get(p, job_id))["result"].get("candidate", {})
        second = (await ops.store.get(p, other_id))["result"].get("candidate", {})
        old = {a["assertion_id"]: a for a in first.get("assertions", [])}
        new = {a["assertion_id"]: a for a in second.get("assertions", [])}
        return {
            "added": [new[k] for k in new.keys() - old.keys()],
            "removed": [old[k] for k in old.keys() - new.keys()],
            "changed": [{"before": old[k], "after": new[k]} for k in old.keys() & new.keys() if old[k] != new[k]],
        }
