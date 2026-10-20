"""PG Dataset heads, immutable revisions, Cases and content-addressed materials."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid

from .._util import normalize_artifact_rel_path, stable_hash
from .schema import normalize_document


class DatasetError(ValueError):
    status_code = 400


class DatasetNotFound(DatasetError):
    status_code = 404


class DatasetConflict(DatasetError):
    status_code = 409


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value):
        raise DatasetError("invalid resource ID")
    return value


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


async def lock(conn, tenant_id, key):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{tenant_id}:replay:{key}")


def decode_materials(files):
    if not isinstance(files, list) or len(files) > 500:
        raise DatasetError("files must contain at most 500 materials")
    result = {}
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "content_b64", "sha256", "size"}:
            raise DatasetError("invalid material file")
        path = normalize_artifact_rel_path(item.get("path"))
        if path in result:
            raise DatasetError("duplicate material path")
        try:
            data = base64.b64decode(item.get("content_b64", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise DatasetError("invalid material base64") from exc
        total += len(data)
        if len(data) > 20 * 1024 * 1024 or total > 64 * 1024 * 1024:
            raise DatasetError("materials exceed size limit")
        if item.get("sha256") and item["sha256"] != hashlib.sha256(data).hexdigest():
            raise DatasetError("material hash mismatch")
        if "size" in item and item["size"] != len(data):
            raise DatasetError("material size mismatch")
        result[path] = data
    return result


class DatasetRepository:
    def __init__(self, tx) -> None:
        self.tx = tx
        self.schema = tx.pool.schema

    async def get(self, conn, dataset_id, revision=None, *, include_deleted=False):
        safe_id(dataset_id)
        head = await conn.fetchrow(f"SELECT * FROM {self.schema}.datasets WHERE dataset_id=$1", dataset_id)
        if head is None or (head["deleted"] and not include_deleted):
            raise DatasetNotFound("Dataset not found")
        version = head["revision"] if revision is None else revision
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.dataset_revisions WHERE dataset_id=$1 AND revision=$2",
            dataset_id, version,
        )
        if row is None:
            raise DatasetNotFound("Dataset revision not found")
        document = unpack(row["document"])
        if stable_hash(document) != row["document_hash"]:
            raise DatasetError("Dataset hash mismatch")
        return {**document, "revision": version, "document_hash": row["document_hash"],
                "case_count": len(document["cases"]), "ingested_at": row["created_at"]}

    async def list(self, conn, *, search="", skill_id="", limit=100, offset=0):
        rows = await conn.fetch(
            f"SELECT d.dataset_id,d.revision,d.document_hash,d.name,d.description,d.created_at,d.updated_at,"
            f"jsonb_array_length(r.document->'cases') AS case_count FROM {self.schema}.datasets d "
            f"JOIN {self.schema}.dataset_revisions r ON r.tenant_id=d.tenant_id "
            "AND r.dataset_id=d.dataset_id AND r.revision=d.revision "
            "WHERE NOT d.deleted AND ($1='' OR d.name ILIKE '%'||$1||'%' OR d.description ILIKE '%'||$1||'%') "
            "AND ($2='' OR r.document->'skills' @> jsonb_build_array(jsonb_build_object('skill_id',$2::text))) "
            "ORDER BY d.updated_at DESC,d.dataset_id LIMIT $3 OFFSET $4",
            search, skill_id, min(500, max(1, limit)), max(0, offset),
        )
        return [dict(row) for row in rows]

    async def count(self, conn, *, search=""):
        return await conn.fetchval(
            f"SELECT count(*) FROM {self.schema}.datasets WHERE NOT deleted "
            "AND ($1='' OR name ILIKE '%'||$1||'%' OR description ILIKE '%'||$1||'%')", search,
        )

    async def historical_cases(self, conn, *, skill_id, before_dataset_id):
        rows = await conn.fetch(
            f"SELECT d.dataset_id,d.name,d.revision,d.document_hash,c.payload AS case "
            f"FROM {self.schema}.datasets d JOIN {self.schema}.dataset_revisions r "
            "ON r.tenant_id=d.tenant_id AND r.dataset_id=d.dataset_id AND r.revision=d.revision "
            f"JOIN {self.schema}.dataset_cases c ON c.tenant_id=d.tenant_id "
            "AND c.dataset_id=d.dataset_id AND c.revision=d.revision "
            "WHERE NOT d.deleted AND d.dataset_id<>$2 "
            f"AND d.created_at <= (SELECT created_at FROM {self.schema}.datasets WHERE dataset_id=$2) "
            "AND r.document->'skills' @> jsonb_build_array(jsonb_build_object('skill_id',$1::text)) "
            "AND (r.document #>> '{source,kind}'='evolution' "
            "OR r.document #>> '{metadata,enabled_for_evolution}'='true') "
            "AND c.payload->'skill_ids' <@ jsonb_build_array($1::text) "
            "ORDER BY d.dataset_id,c.case_id",
            skill_id, safe_id(before_dataset_id),
        )
        return [{**dict(row), "case": unpack(row["case"])} for row in rows]

    async def save(self, conn, *, tenant_id, document, files=None, expected_revision=None):
        document = normalize_document(document, allow_empty=True)
        dataset_id = safe_id(document["dataset_id"])
        if len(document["cases"]) > 500 or len(json.dumps(document).encode()) > 64 * 1024 * 1024:
            raise DatasetError("Dataset exceeds 500 Cases or 64 MiB")
        if not 1 <= len(document["name"]) <= 120 or len(document["description"]) > 2000:
            raise DatasetError("invalid Dataset name or description")
        await lock(conn, tenant_id, "dataset:" + dataset_id)
        head = await conn.fetchrow(f"SELECT * FROM {self.schema}.datasets WHERE dataset_id=$1", dataset_id)
        current = head["revision"] if head else 0
        if head and head["deleted"]:
            raise DatasetConflict("Dataset has been deleted")
        if expected_revision is None and head:
            raise DatasetConflict("expected_revision is required when updating a Dataset")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != current):
            raise DatasetConflict("Dataset revision changed")
        decoded = decode_materials(files) if files is not None else {}
        for case in document["cases"]:
            seen = set()
            for material in case["materials"]:
                path = normalize_artifact_rel_path(material["path"])
                if path in seen:
                    raise DatasetError("duplicate material reference")
                seen.add(path)
                if path in decoded:
                    data = decoded[path]
                    sha = hashlib.sha256(data).hexdigest()
                    if material.get("sha256") and material["sha256"] != sha:
                        raise DatasetError("material reference hash mismatch")
                    material.update(path=path, sha256=sha, size=len(data))
                    await conn.execute(
                        f"INSERT INTO {self.schema}.dataset_materials(tenant_id,sha256,content,size) "
                        "VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING", tenant_id, sha, data, len(data),
                    )
                else:
                    await self.material(conn, material)
        revision = current + 1
        now = (await conn.fetchval("SELECT clock_timestamp()")).isoformat()
        document["created_at"] = (await self.get(conn, dataset_id))["created_at"] if head else document["created_at"]
        document["updated_at"] = now
        digest = stable_hash(document)
        await conn.execute(
            f"INSERT INTO {self.schema}.datasets(tenant_id,dataset_id,revision,name,description,document_hash) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,dataset_id) DO UPDATE SET "
            "revision=EXCLUDED.revision,name=EXCLUDED.name,description=EXCLUDED.description,"
            "document_hash=EXCLUDED.document_hash,updated_at=now()",
            tenant_id, dataset_id, revision, document["name"], document["description"], digest,
        )
        await conn.execute(
            f"INSERT INTO {self.schema}.dataset_revisions(tenant_id,dataset_id,revision,document,document_hash) "
            "VALUES($1,$2,$3,$4::jsonb,$5)", tenant_id, dataset_id, revision, json.dumps(document), digest,
        )
        for position, case in enumerate(document["cases"]):
            await conn.execute(
                f"INSERT INTO {self.schema}.dataset_cases "
                "(tenant_id,dataset_id,revision,case_id,position,case_hash,payload) "
                "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
                tenant_id, dataset_id, revision, safe_id(case["case_id"]),
                position, stable_hash(case), json.dumps(case),
            )
        return await self.get(conn, dataset_id)

    async def material(self, conn, record):
        normalize_artifact_rel_path(record["path"])
        sha = record.get("sha256", "")
        row = await conn.fetchrow(f"SELECT content,size FROM {self.schema}.dataset_materials WHERE sha256=$1", sha)
        if row is None:
            raise DatasetError("Dataset material unavailable")
        data = bytes(row["content"])
        if hashlib.sha256(data).hexdigest() != sha or row["size"] != len(data):
            raise DatasetError("Dataset material hash mismatch")
        if record.get("size", len(data)) != len(data):
            raise DatasetError("Dataset material size mismatch")
        return {**record, "sha256": sha, "size": len(data), "content_b64": base64.b64encode(data).decode()}

    async def freeze(self, conn, dataset_id, revision=None):
        saved = await self.get(conn, dataset_id, revision)
        document = {key: value for key, value in saved.items()
                    if key not in {"revision", "document_hash", "case_count", "ingested_at"}}
        hydrated = json.loads(json.dumps(document))
        for case in hydrated["cases"]:
            case["materials"] = [await self.material(conn, item) for item in case["materials"]]
        return {"document": document, "document_hash": saved["document_hash"], "revision": saved["revision"],
                "execution_document": hydrated, "execution_hash": stable_hash(hydrated)}

    async def delete(self, conn, *, tenant_id, dataset_id):
        await lock(conn, tenant_id, "dataset:" + safe_id(dataset_id))
        await self.get(conn, dataset_id)
        await conn.execute(f"UPDATE {self.schema}.datasets SET deleted=TRUE,updated_at=now() WHERE dataset_id=$1",
                           dataset_id)

    @staticmethod
    def new_id():
        return "ds_" + uuid.uuid4().hex
