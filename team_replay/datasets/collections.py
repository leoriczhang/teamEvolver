"""Session snapshot collections; the host supplies tenant-scoped local storage."""
from __future__ import annotations

import copy
import io
import json
import re
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

from .schema import (
    DATASET_SCHEMA_V2,
    legacy_document_view,
    normalize_document,
    validate_document,
)

MAX_ITEMS = 500
MAX_DATASET_BYTES = 64 * 1024 * 1024
ACTIVE = {"queued", "running", "cancelling"}
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}$")


class DatasetNotFound(LookupError):
    pass


class DatasetConflict(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("无效的数据集、Session 或运行 ID")
    return value


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def item_summary(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "session"}


def snapshot_ref(case: dict) -> str:
    provenance = case.get("provenance") or {}
    value = str(provenance.get("snapshot_ref") or "")
    if not value:
        return ""
    expected = f"snapshots/{safe_id(str(case.get('case_id') or ''))}.json"
    if value != expected:
        raise ValueError("无效的 Session 快照引用")
    return value


def snapshot_item(session: dict, row: dict) -> dict:
    """Consume canonical adapter output, never re-parse upstream traces."""
    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, dict)]
    first = next((turn for turn in turns if str(turn.get("prompt_text") or "").strip()), {})
    query = str(first.get("prompt_text") or "").strip()
    if not query:
        messages = session.get("messages") or [m for turn in turns for m in turn.get("messages") or []]
        query = next((
            m["content"].strip() for m in messages
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
            and m["content"].strip()
        ), "")
    meta = row.get("meta") or session.get("meta") or {}
    used_skills = list(dict.fromkeys(
        str(item or "").strip()
        for item in row.get("used_skills") or session.get("used_skills") or []
        if str(item or "").strip()
    ))
    return {
        "item_id": "item_" + uuid.uuid4().hex,
        "session_id": safe_id(str(session.get("session_id") or row.get("session_id") or "")),
        "trace_id": str(meta.get("trace_id") or ""),
        "title": str(row.get("title") or session.get("title") or query),
        "query": query,
        "skill_ids": used_skills,
        # Use the explicit user request as a completion criterion until edited.
        "requirements": [query] if query else [], "requirements_source": "initial_query",
        "turn_num": first.get("turn_num") or 1,
        "timestamp": row.get("timestamp") or session.get("timestamp") or "",
        "ingested_at": row.get("ingested_at") or session.get("ingested_at") or "",
        "used_skills": used_skills,
        "user_alias": row.get("user_alias") or session.get("user_alias") or "",
        "judge": copy.deepcopy(row.get("judge") or session.get("judge") or {}),
        "session": copy.deepcopy(session),
    }


class DatasetCollectionStore:
    def __init__(self, bucket: Any, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = f"{prefix}session_datasets/"
        self.lock = threading.RLock()

    def key(self, dataset_id: str, suffix: str) -> str:
        return f"{self.prefix}{safe_id(dataset_id)}/{suffix}"

    def read(self, key: str) -> dict:
        try:
            return json.loads(self.bucket.get_object(key).read())
        except FileNotFoundError as exc:
            raise DatasetNotFound("数据集或运行记录不存在") from exc

    def write(self, key: str, value: dict) -> None:
        self.bucket.put_object(key, encode(value))

    def metadata(self, dataset_id: str) -> dict:
        return self.read(self.key(dataset_id, "metadata.json"))

    def load_document(self, dataset_id: str) -> dict:
        return normalize_document(
            self.read(self.key(dataset_id, "dataset.json")),
            default_dataset_id=dataset_id,
            default_subject={"kind": "session"},
        )

    def present_document(
        self,
        dataset_id: str,
        document: dict,
        *,
        run_id: str = "",
    ) -> dict:
        hydrated = copy.deepcopy(normalize_document(
            document,
            default_dataset_id=dataset_id,
            default_subject={"kind": "session"},
        ))
        for case in hydrated["cases"]:
            provenance = case.get("provenance") or {}
            ref = snapshot_ref(case)
            if ref and "session_snapshot" not in provenance:
                snapshot_key = (
                    self.run_key(dataset_id, run_id, ref)
                    if run_id
                    else self.key(dataset_id, ref)
                )
                provenance["session_snapshot"] = self.read(
                    snapshot_key
                )
        return legacy_document_view(hydrated, item_key="items")

    def load(self, dataset_id: str) -> dict:
        return self.present_document(dataset_id, self.load_document(dataset_id))

    def list(self) -> list[dict]:
        with self.lock:
            rows = [
                self.read(obj.key) for obj in self.bucket.iter_objects(prefix=self.prefix)
                if obj.key.endswith("/metadata.json") and "/runs/" not in obj.key
            ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def save(self, dataset: dict) -> dict:
        document = normalize_document(
            dataset,
            default_dataset_id=str(dataset.get("dataset_id") or ""),
            default_name=str(dataset.get("name") or ""),
            default_subject={"kind": "session"},
        )
        errors = validate_document(document)
        if errors:
            raise ValueError("数据集格式无效：" + "；".join(errors))
        snapshots: list[tuple[str, dict[str, Any]]] = []
        for case in document["cases"]:
            provenance = case.get("provenance") or {}
            snapshot = provenance.pop("session_snapshot", None)
            if isinstance(snapshot, dict):
                ref = f"snapshots/{safe_id(case['case_id'])}.json"
                provenance["snapshot_ref"] = ref
                snapshots.append((ref, snapshot))
            else:
                snapshot_ref(case)
        body = encode(document)
        total_size = len(body) + sum(len(encode(snapshot)) for _, snapshot in snapshots)
        if total_size > MAX_DATASET_BYTES:
            raise ValueError("数据集超过 64 MiB，请减少选择的 Session 数量")
        if len(document["cases"]) > MAX_ITEMS:
            raise ValueError(f"每个数据集最多 {MAX_ITEMS} 条 Session")
        metadata = {key: value for key, value in document.items() if key != "cases"}
        metadata["item_count"] = len(document["cases"])
        for ref, snapshot in snapshots:
            self.write(self.key(document["dataset_id"], ref), snapshot)
        self.bucket.put_object(self.key(document["dataset_id"], "dataset.json"), body)
        self.write(self.key(document["dataset_id"], "metadata.json"), metadata)
        active_snapshot_keys = {
            self.key(
                document["dataset_id"],
                snapshot_ref(case),
            )
            for case in document["cases"]
            if snapshot_ref(case)
        }
        snapshot_prefix = self.key(document["dataset_id"], "snapshots/")
        for obj in list(self.bucket.iter_objects(prefix=snapshot_prefix)):
            if obj.key not in active_snapshot_keys:
                self.bucket.delete_object(obj.key)
        return metadata

    def create(self, name: str, description: str, items: list[dict], source: dict) -> dict:
        name = name.strip()
        if not name or len(name) > 120 or len(description) > 2000:
            raise ValueError("名称需为 1–120 字，描述不能超过 2000 字")
        if not items:
            raise ValueError("请选择至少一条有正文的 Session")
        skill_ids = sorted({
            str(skill_id)
            for item in items
            for skill_id in item.get("skill_ids") or []
            if str(skill_id)
        })
        with self.lock:
            return self.save({
                "schema_version": DATASET_SCHEMA_V2,
                "dataset_id": "ds_" + uuid.uuid4().hex,
                "name": name, "description": description.strip(),
                "created_at": now(), "updated_at": now(), "source": source,
                "skills": skill_ids, "cases": items,
            })

    def assert_idle(self, dataset_id: str) -> None:
        if any(run["status"] in ACTIVE for run in self.runs(dataset_id)):
            raise DatasetConflict("数据集正在重回放，请等待任务结束或停止任务后再修改")

    def update(self, dataset_id: str, changes: dict, additions: list[dict] | None = None) -> dict:
        with self.lock:
            self.assert_idle(dataset_id)
            document = self.load_document(dataset_id)
            dataset = self.present_document(dataset_id, document)
            name = str(changes.get("name", dataset["name"])).strip()
            description = str(changes.get("description", dataset["description"])).strip()
            if not name or len(name) > 120 or len(description) > 2000:
                raise ValueError("名称需为 1–120 字，描述不能超过 2000 字")
            dataset.update(name=name, description=description, updated_at=now())
            existing = {item["session_id"] for item in dataset["items"]}
            for item in additions or []:
                if item["session_id"] not in existing:
                    dataset["items"].append(item)
                    existing.add(item["session_id"])
            return self.save(dataset)

    def change_item(self, dataset_id: str, item_id: str, changes: dict | None) -> dict:
        with self.lock:
            self.assert_idle(dataset_id)
            dataset = self.load(dataset_id)
            item = next((item for item in dataset["items"] if item["item_id"] == item_id), None)
            if item is None:
                raise DatasetNotFound("数据集条目不存在")
            if changes is None:
                dataset["items"].remove(item)
            else:
                query = str(changes.get("query", item["query"])).strip()
                requirements = changes.get("requirements", item["requirements"])
                if (
                    not query or len(query) > 100000 or not isinstance(requirements, list)
                    or not 1 <= len(requirements) <= 100
                    or any(not isinstance(text, str) or not text.strip() or len(text) > 100000
                           for text in requirements)
                ):
                    raise ValueError("请填写 Query 和 1–100 条非空 Checklist")
                item.update(query=query, requirements=[text.strip() for text in requirements],
                            requirements_source="manual")
                item.pop("checks", None)
                item.pop("checklist", None)
            dataset["updated_at"] = now()
            return self.save(dataset)

    def delete(self, dataset_id: str) -> None:
        with self.lock:
            self.metadata(dataset_id)
            self.assert_idle(dataset_id)
            for obj in list(self.bucket.iter_objects(prefix=self.key(dataset_id, ""))):
                self.bucket.delete_object(obj.key)

    def run_key(self, dataset_id: str, run_id: str, suffix: str = "metadata.json") -> str:
        return self.key(dataset_id, f"runs/{safe_id(run_id)}/{suffix}")

    def runs(self, dataset_id: str) -> list[dict]:
        rows = [
            self.read(obj.key) for obj in self.bucket.iter_objects(prefix=self.key(dataset_id, "runs/"))
            if obj.key.endswith("/metadata.json")
        ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def run(self, dataset_id: str, run_id: str) -> dict:
        return self.read(self.run_key(dataset_id, run_id))

    def save_run(self, run: dict) -> None:
        self.write(self.run_key(run["dataset_id"], run["run_id"]), run)

    def create_run(self, dataset_id: str, options: dict, owner_id: str) -> tuple[dict, list[dict]]:
        with self.lock:
            self.assert_idle(dataset_id)
            document = self.load_document(dataset_id)
            dataset = self.present_document(dataset_id, document)
            if not dataset["items"]:
                raise ValueError("空数据集无法重回放")
            if any(not item["query"].strip() or not item["requirements"] for item in dataset["items"]):
                raise ValueError("部分条目缺少 Query 或 Checklist，请先编辑补齐")
            run_id = "batch_" + uuid.uuid4().hex
            run = {
                "run_id": run_id, "dataset_id": dataset_id, "dataset_name": dataset["name"],
                "status": "queued", "owner_id": owner_id, "created_at": now(), "finished_at": "",
                "options": options, "total": len(dataset["items"]), "completed": 0,
                "succeeded": 0, "failed": 0, "skipped": 0,
                "items": [{
                    "item_id": item["item_id"], "session_id": item["session_id"], "trace_id": item["trace_id"],
                    "query": item["query"], "status": "queued", "success": None,
                } for item in dataset["items"]],
            }
            run_document = copy.deepcopy(document)
            for case in run_document["cases"]:
                ref = snapshot_ref(case)
                if not ref:
                    continue
                snapshot = self.bucket.get_object(
                    self.key(dataset_id, ref)
                ).read()
                self.bucket.put_object(
                    self.run_key(dataset_id, run_id, ref),
                    snapshot,
                )
            self.write(self.run_key(dataset_id, run_id, "input.json"), run_document)
            self.save_run(run)
            return run, dataset["items"]

    def request_cancel(self, dataset_id: str, run_id: str) -> dict:
        with self.lock:
            run = self.run(dataset_id, run_id)
            if run["status"] in ACTIVE:
                run["status"] = "cancelling"
                self.save_run(run)
            return run

    def item_running(self, dataset_id: str, run_id: str, item_id: str) -> bool:
        with self.lock:
            run = self.run(dataset_id, run_id)
            if run["status"] not in {"queued", "running"}:
                return False
            run["status"] = "running"
            run.setdefault("started_at", now())
            next(item for item in run["items"] if item["item_id"] == item_id)["status"] = "running"
            self.save_run(run)
            return True

    def finish_item(self, dataset_id: str, run_id: str, item_id: str, result: dict) -> None:
        with self.lock:
            run = self.run(dataset_id, run_id)
            row = next(item for item in run["items"] if item["item_id"] == item_id)
            if row["status"] not in {"running", "queued"}:
                return
            status = "completed" if result.get("ok") else (
                "skipped" if result.get("status") == "unsupported" else "failed"
            )
            row.update(
                status=status, success=bool(result.get("completed")) if result.get("ok") else None,
                replay_trace_id=result.get("request_id") or "", error=result.get("error") or "",
                finished_at=now(),
            )
            self.write(self.run_key(dataset_id, run_id, f"results/{safe_id(item_id)}.json"), result)
            run["completed"] += 1
            run["succeeded"] += int(row["success"] is True)
            run["failed"] += int(status == "failed" or row["success"] is False)
            run["skipped"] += int(status == "skipped")
            self.save_run(run)

    def finish_run(self, dataset_id: str, run_id: str, *, interrupted: bool = False, error: str = "") -> None:
        with self.lock:
            run = self.run(dataset_id, run_id)
            unfinished = [item for item in run["items"] if item["status"] in {"queued", "running"}]
            status = "interrupted" if interrupted else (
                "cancelled" if run["status"] == "cancelling" else "completed"
            )
            for item in unfinished:
                item.update(status="interrupted" if interrupted else "cancelled", success=None)
            run.update(status=status, finished_at=now())
            if error:
                run["error"] = error
            self.save_run(run)

    def recover(self, owner_id: str) -> None:
        with self.lock:
            for dataset in self.list():
                for run in self.runs(dataset["dataset_id"]):
                    if run["status"] in ACTIVE and run.get("owner_id") != owner_id:
                        self.finish_run(dataset["dataset_id"], run["run_id"], interrupted=True,
                                        error="服务已重启，本次任务中断；已完成结果仍可查看")

    def export_zip(self, dataset_id: str) -> io.BytesIO:
        with self.lock:
            document = self.load_document(dataset_id)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                exported_document = copy.deepcopy(document)
                exported_document["metadata"] = {
                    **exported_document.get("metadata", {}),
                    "item_count": len(document["cases"]),
                }
                archive.writestr("dataset.json", encode(exported_document))
                archive.writestr("cases.jsonl", "".join(
                    json.dumps(case, ensure_ascii=False) + "\n" for case in document["cases"]
                ).encode("utf-8"))
                for case in document["cases"]:
                    ref = snapshot_ref(case)
                    if ref:
                        archive.writestr(
                            ref,
                            self.bucket.get_object(
                                self.key(dataset_id, ref)
                            ).read(),
                        )
                archive.writestr("README.txt", (
                    "dataset.json：数据集元信息与创建时筛选条件\n"
                    "cases.jsonl：Query、Checklist、来源 Session/Trace、业务时间与入库时间\n"
                    "snapshots/<case_id>.json：创建时保存的完整 Session 快照\n"
                ).encode("utf-8"))
            output.seek(0)
            return output
