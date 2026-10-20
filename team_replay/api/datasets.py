"""Dataset API backed by immutable PG revisions and persistent Worker tasks."""
from __future__ import annotations

import io
import json
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from team_skills.library.queries import pinned_treatment
from teamEvolver.tenants.registry import current_tenant_id

from ..commands import enqueue
from ..config import run_options
from ..datasets.repository import DatasetRepository
from ..datasets.schema import make_document, normalize_document
from ..datasets.selection import select_cases
from ..queries import present_run
from ..repository import ReplayRepository
from .common import ERRORS, actor, body, error, identity


def create_router(services):
    router = APIRouter(tags=["datasets"])
    repo = DatasetRepository(services.tx)
    runs = ReplayRepository(services.tx)
    tx = services.tx

    @router.get("/api/datasets")
    async def listing(search: str = "", limit: int = 100, offset: int = 0):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await repo.list(conn, search=search, limit=limit, offset=offset)
            total = await repo.count(conn, search=search)
        return {"datasets": rows, "total": total, "limit": limit, "offset": offset}

    @router.post("/api/datasets", status_code=201)
    async def save(request: Request):
        actor(request)
        raw = await body(request)
        files = raw.pop("files", None)
        expected = raw.pop("expected_revision", None)
        try:
            if "session_ids" in raw or "filters" in raw:
                if set(raw) - {"name", "description", "session_ids", "filters"}:
                    raise ValueError("unsupported Dataset selection field")
                async with tx.tenant_tx(current_tenant_id()) as conn:
                    cases = await select_cases(conn, services, {
                        key: raw[key] for key in ("session_ids", "filters") if key in raw
                    })
                    skills = sorted({skill for case in cases for skill in case["skill_ids"]})
                    document = make_document(
                        dataset_id=repo.new_id(), name=raw["name"], description=raw.get("description", ""),
                        cases=cases, skills=skills, source={"kind": "sessions", "filters": raw.get("filters", {})},
                    )
                    return await repo.save(conn, tenant_id=current_tenant_id(), document=document)
            document = normalize_document(raw, default_dataset_id=repo.new_id(), allow_empty=True)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await repo.save(conn, tenant_id=current_tenant_id(), document=document,
                                       files=files, expected_revision=expected)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}")
    async def detail(dataset_id: str, search: str = "", limit: int = 100, offset: int = 0):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                document = await repo.get(conn, dataset_id)
            rows = [case for case in document["cases"] if search.casefold() in case["query"].casefold()]
            return {**document, "cases": rows[max(0, offset):max(0, offset) + min(500, max(1, limit))],
                    "total": len(rows)}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.patch("/api/datasets/{dataset_id}")
    async def update(dataset_id: str, request: Request):
        actor(request)
        raw = await body(request)
        expected = raw.pop("expected_revision", None)
        if set(raw) - {"name", "description", "skills", "cases"}:
            raise HTTPException(400, "unsupported Dataset edit")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await repo.freeze(conn, dataset_id)
                document = {**frozen["document"], **raw}
                return await repo.save(conn, tenant_id=current_tenant_id(), document=document,
                                       expected_revision=expected)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}/cases/{case_id}")
    async def case_detail(dataset_id: str, case_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                document = await repo.get(conn, dataset_id)
            return next(case for case in document["cases"] if case["case_id"] == case_id)
        except StopIteration as exc:
            raise HTTPException(404, "Case not found") from exc
        except ERRORS as exc:
            raise error(exc) from exc

    @router.post("/api/datasets/{dataset_id}/cases")
    async def add_cases(dataset_id: str, request: Request):
        actor(request)
        raw = await body(request)
        expected = raw.pop("expected_revision", None)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await repo.freeze(conn, dataset_id)
                document = frozen["document"]
                additions = await select_cases(conn, services, raw)
                source_ids = {c["provenance"].get("session_id") for c in document["cases"]}
                document["cases"].extend(c for c in additions if c["provenance"].get("session_id") not in source_ids)
                skills = sorted({s for c in document["cases"] for s in c["skill_ids"]})
                document["skills"] = [{"skill_id": s} for s in skills]
                return await repo.save(conn, tenant_id=current_tenant_id(), document=document,
                                       expected_revision=expected)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.patch("/api/datasets/{dataset_id}/cases/{case_id}")
    async def edit_case(dataset_id: str, case_id: str, request: Request):
        return await change_case(dataset_id, case_id, request, delete=False)

    @router.delete("/api/datasets/{dataset_id}/cases/{case_id}")
    async def remove_case(dataset_id: str, case_id: str, request: Request):
        return await change_case(dataset_id, case_id, request, delete=True)

    async def change_case(dataset_id, case_id, request, delete):
        actor(request)
        raw = await body(request)
        expected = raw.pop("expected_revision", None)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await repo.freeze(conn, dataset_id)
                document = frozen["document"]
                index = next(i for i, case in enumerate(document["cases"]) if case["case_id"] == case_id)
                if delete:
                    if raw:
                        raise ValueError("delete accepts only expected_revision")
                    document["cases"].pop(index)
                else:
                    document["cases"][index] = {**document["cases"][index], **raw}
                return await repo.save(conn, tenant_id=current_tenant_id(), document=document,
                                       expected_revision=expected)
        except StopIteration as exc:
            raise HTTPException(404, "Case not found") from exc
        except ERRORS as exc:
            raise error(exc) from exc

    @router.delete("/api/datasets/{dataset_id}")
    async def remove(dataset_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await repo.delete(conn, tenant_id=current_tenant_id(), dataset_id=dataset_id)
            return {"deleted": True, "dataset_id": dataset_id}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}/export")
    async def export(dataset_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await repo.freeze(conn, dataset_id)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("dataset.json", json.dumps(frozen["execution_document"], ensure_ascii=False))
            return Response(output.getvalue(), media_type="application/zip",
                            headers={"Content-Disposition": "attachment; filename=dataset.zip"})
        except ERRORS as exc:
            raise error(exc) from exc

    @router.post("/api/datasets/{dataset_id}/runs", status_code=202)
    async def start_run(dataset_id: str, request: Request):
        actor(request)
        raw = await body(request)
        try:
            options = run_options(raw)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await repo.freeze(conn, dataset_id)
                if not frozen["document"]["cases"]:
                    raise ValueError("Dataset run requires at least one valid Case")
                pinned = await pinned_treatment(conn, services.tx, frozen["document"]["skills"])
                frozen["skill_treatment"] = pinned["treatment"]
                frozen["skill_versions"] = pinned["versions"]
                record = await enqueue(
                    conn, services, tenant_id=current_tenant_id(), domain="dataset",
                    run_id=identity(request, "dataset"), subject_id=dataset_id,
                    request={"dataset_id": dataset_id, "options": options}, dataset=frozen, options=options,
                )
                return await present_run(conn, services, "dataset", record)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}/runs")
    async def run_list(dataset_id: str, limit: int = 100, offset: int = 0):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await repo.get(conn, dataset_id)
                rows = await runs.list(conn, "dataset", dataset_id=dataset_id, limit=limit, offset=offset)
                total = await runs.count(conn, "dataset", dataset_id=dataset_id)
                return {"runs": [await present_run(conn, services, "dataset", row) for row in rows], "total": total}
        except ERRORS as exc:
            raise error(exc) from exc

    async def get_run(conn, dataset_id, run_id):
        record = await runs.get(conn, "dataset", run_id)
        if record["dataset_id"] != dataset_id:
            raise HTTPException(404, "Dataset run not found")
        return record

    @router.get("/api/datasets/{dataset_id}/runs/{run_id}")
    async def run_detail(dataset_id: str, run_id: str, limit: int = 100, offset: int = 0):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                presented = await present_run(conn, services, "dataset", await get_run(conn, dataset_id, run_id))
                presented["cases"] = presented["cases"][max(0, offset):max(0, offset) + min(500, max(1, limit))]
                return presented
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}/runs/{run_id}/cases/{case_id}")
    async def run_case(dataset_id: str, run_id: str, case_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                record = await get_run(conn, dataset_id, run_id)
            return next(c for c in record["input"]["dataset"]["execution_document"]["cases"] if c["case_id"] == case_id)
        except StopIteration as exc:
            raise HTTPException(404, "saved Case not found") from exc
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/datasets/{dataset_id}/runs/{run_id}/results/{case_id}")
    async def run_result(dataset_id: str, run_id: str, case_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await get_run(conn, dataset_id, run_id)
                results = await runs.cases(conn, "dataset", run_id)
            if case_id not in results:
                raise HTTPException(404, "Case result not available")
            return results[case_id]
        except ERRORS as exc:
            raise error(exc) from exc

    @router.post("/api/datasets/{dataset_id}/runs/{run_id}/cancel")
    async def cancel(dataset_id: str, run_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                record = await get_run(conn, dataset_id, run_id)
                await services.tasks.request_cancel(conn, task_id=run_id)
                return await present_run(conn, services, "dataset", record)
        except ERRORS as exc:
            raise error(exc) from exc
    return router
