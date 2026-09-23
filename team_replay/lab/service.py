"""Developer-facing skill experiments backed by True Replay.

The skill lab keeps three concerns separate:

* editable datasets (query, requirements, trajectory requirements, materials)
* read-only datasets projected from evolution replay cases
* durable experiment runs with full branch traces and objective efficiency data

Dataset and run artifacts use the same object-storage boundary as sessions and
validation jobs. When no OpenViking backend is configured, an ephemeral
in-memory fallback keeps the lab usable within a single process run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Optional

import yaml

from team_replay.datasets.store import (
    SkillDatasetStore,
    dataset_material_integrity,
)
from team_replay.datasets.schema import (
    DATASET_SCHEMA_V2,
    legacy_case_view,
    normalize_case,
    normalize_skill_refs,
)
from team_replay.datasets.synthesis import checklist_items, flatten_requirements
from teamEvolver.storage import InMemoryObjectStore, is_not_found_error

logger = logging.getLogger(__name__)


_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_DATASET_BYTES = 80 * 1024 * 1024
_CORE_SKILL_KEYS = {"name", "description", "category"}


# Skill Library access is resolved lazily so team_replay keeps no import-time
# dependency on the Skill-evolution package (one-way dependency rule).
class _LazyModule:
    def __init__(self, dotted: str) -> None:
        self._dotted = dotted
        self._mod = None

    def __getattr__(self, name: str):
        if self._mod is None:
            from importlib import import_module

            self._mod = import_module(self._dotted)
        return getattr(self._mod, name)


editor = _LazyModule("team_skills.library.editor")
frontmatter = _LazyModule("team_skills.library.frontmatter")


def attach_bundle_payload(*args, **kwargs):
    from team_skills.library.bundle import attach_bundle_payload as _impl

    return _impl(*args, **kwargs)


def _load_skill_bundle(
    load_bundle: Callable[[str], Mapping[str, bytes]], name: str
) -> Mapping[str, bytes]:
    """Resolve one skill's bundle, mapping a missing skill to a lab error."""
    try:
        bundle = load_bundle(name)
    except FileNotFoundError as exc:
        raise SkillLabError(f"Skill 不存在：{name}") from exc
    if not isinstance(bundle, Mapping) or not bundle.get("SKILL.md"):
        raise SkillLabError(f"Skill 缺少 SKILL.md：{name}")
    return bundle


class SkillLabError(ValueError):
    """Raised when a skill-lab request is malformed or unsafe."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_block(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item or "").strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


def _section_body(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^###\s*{re.escape(heading)}\s*$\s*(.*?)(?=^###\s+|\Z)"
    )
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""


def parse_dataset_markdown(markdown: str) -> dict[str, str]:
    """Parse the dataset format used by agent_evolve_evaluation."""
    raw = str(markdown or "").strip()
    query = _section_body(raw, "query")
    requirements = _section_body(raw, "要求")
    trajectory_requirements = _section_body(raw, "轨迹要求")
    if not query:
        raise SkillLabError("数据集 Markdown 缺少 `### query` 或 query 内容为空")
    return {
        "query": query,
        "requirements": requirements,
        "trajectory_requirements": trajectory_requirements,
    }


def render_dataset_markdown(dataset: Mapping[str, Any]) -> str:
    """Render one dataset in the portable Markdown contract."""
    parts = ["### query", "", _text_block(dataset.get("query"))]
    requirements = _text_block(dataset.get("requirements"))
    trajectory = _text_block(dataset.get("trajectory_requirements"))
    if requirements:
        parts.extend(["", "### 要求", "", requirements])
    if trajectory:
        parts.extend(["", "### 轨迹要求", "", trajectory])
    return "\n".join(parts).rstrip() + "\n"


def compose_experiment_instruction(dataset: Mapping[str, Any]) -> str:
    """Return only the initial query; requirements stay hidden until disclosed."""
    return _text_block(dataset.get("query"))


def _normalize_material_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if (
        not raw
        or raw.startswith("/")
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SkillLabError(f"不安全的材料路径：{value!r}")
    return "/".join(parts)


def _normalize_dataset_id(value: Any) -> str:
    dataset_id = str(value or "").strip()
    if not _DATASET_ID_RE.fullmatch(dataset_id):
        raise SkillLabError("dataset_id 只能包含字母、数字、点、短横线和下划线")
    return dataset_id


def _dataset_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"ds-{stamp}-{uuid.uuid4().hex[:8]}"


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


class SkillLabStore:
    """Persist skill-lab datasets, materials, and experiment runs."""

    def __init__(self, bucket) -> None:
        self._bucket = bucket
        self._tenant_id: str = "default"
        self._datasets = SkillDatasetStore(bucket, prefix=self._dataset_store_prefix())

    # Process-lifetime fallback store shared across from_config() calls, used
    # only when no OpenViking backend is configured. Data is ephemeral (in
    # memory) — configure cloud or local OpenViking to persist skill-lab state.
    _fallback_bucket: "InMemoryObjectStore | None" = None

    @classmethod
    def from_config(cls, config, tenant_id: str = "default") -> "SkillLabStore":
        from team_skills.library.hub import SkillHub

        hub = SkillHub.object_storage_from_config(config, tenant_id=tenant_id)
        bucket = hub._bucket if hub is not None else cls._get_fallback_bucket()
        store = cls.__new__(cls)
        store._bucket = bucket
        store._tenant_id = str(tenant_id or "default")
        store._datasets = SkillDatasetStore(bucket, prefix=store._dataset_store_prefix())
        if hub is None:
            logger.warning(
                "[SkillLabStore] no OpenViking backend configured; using an "
                "in-memory fallback store. Skill-lab data will not persist across "
                "restarts. Configure cloud or local OpenViking to persist."
            )
        return store

    @classmethod
    def _get_fallback_bucket(cls):
        if cls._fallback_bucket is None:
            cls._fallback_bucket = InMemoryObjectStore("skill_lab")
        return cls._fallback_bucket

    @staticmethod
    def make_run_id() -> str:
        return _run_id()

    def _dataset_store_prefix(self) -> str:
        """Prefix for SkillDatasetStore (tenant-scoped)."""
        if self._tenant_id and self._tenant_id != "default":
            return f"tenants/{self._tenant_id}/"
        return ""

    def _tenant_prefix(self) -> str:
        """Key prefix for tenant-scoped skill-lab data."""
        if self._tenant_id and self._tenant_id != "default":
            return f"tenants/{self._tenant_id}/skill_lab/"
        return "skill_lab/"

    def _legacy_dataset_key(self, dataset_id: str) -> str:
        return f"{self._tenant_prefix()}datasets/{_normalize_dataset_id(dataset_id)}/metadata.json"

    def _legacy_dataset_prefix(self, dataset_id: str) -> str:
        return f"{self._tenant_prefix()}datasets/{_normalize_dataset_id(dataset_id)}/"

    def _legacy_material_key(self, dataset_id: str, rel_path: str) -> str:
        return (
            f"{self._legacy_dataset_prefix(dataset_id)}materials/"
            f"{_normalize_material_path(rel_path)}"
        )

    def _run_key(self, run_id: str) -> str:
        return f"{self._tenant_prefix()}runs/{_normalize_dataset_id(run_id)}/metadata.json"

    def _run_result_key(self, run_id: str) -> str:
        return f"{self._tenant_prefix()}runs/{_normalize_dataset_id(run_id)}/result.json"

    def _read_json(self, key: str) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(self._bucket.get_object(key).read().decode("utf-8"))
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise
        return value if isinstance(value, dict) else None

    def _write_json(self, key: str, payload: Mapping[str, Any]) -> None:
        self._bucket.put_object(
            key,
            json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _available_material_paths(
        self,
        dataset: Mapping[str, Any],
    ) -> list[str]:
        skill_name = str(dataset.get("skill_name") or "")
        dataset_id = str(dataset.get("dataset_id") or "")
        canonical = self._datasets.load_document(
            skill_name=skill_name,
            dataset_id=dataset_id,
        )
        if canonical:
            return self._datasets.available_material_paths(canonical)
        paths: list[str] = []
        for item in dataset.get("materials") or []:
            if not isinstance(item, Mapping) or not item.get("path"):
                continue
            rel_path = _normalize_material_path(item.get("path"))
            try:
                self._bucket.get_object(
                    self._legacy_material_key(dataset_id, rel_path)
                )
            except Exception as exc:
                if is_not_found_error(exc):
                    continue
                raise
            paths.append(rel_path)
        return paths

    def _present_dataset(
        self,
        dataset: Mapping[str, Any],
    ) -> dict[str, Any]:
        item = {
            **dict(dataset),
            "requirements": _text_block(dataset.get("requirements")),
            "trajectory_requirements": _text_block(
                dataset.get("trajectory_requirements")
            ),
        }
        return {
            **item,
            "material_integrity": dataset_material_integrity(
                item,
                available_paths=self._available_material_paths(item),
            ),
            "dataset_markdown": render_dataset_markdown(item),
        }

    def save_dataset(
        self,
        payload: Mapping[str, Any],
        *,
        files: Optional[list[Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        context_skill = str(payload.get("skill_name") or "").strip()
        if context_skill:
            context_skill = editor.validate_skill_name(context_skill)
        query = _text_block(payload.get("query"))
        if not query:
            raise SkillLabError("query 不能为空")
        dataset_id = (
            _normalize_dataset_id(payload.get("dataset_id"))
            if payload.get("dataset_id")
            else _dataset_id()
        )
        canonical_existing = self._datasets.load_dataset(
            skill_name=context_skill,
            dataset_id=dataset_id,
        )
        existing = (
            canonical_existing
            or self._datasets.find_dataset(dataset_id)
            or self.load_dataset(
                dataset_id,
                skill_name=context_skill,
            )
        )
        explicit_bindings = "skills" in payload or "skill_ids" in payload
        raw_bindings = (
            payload.get("skills")
            if "skills" in payload
            else payload.get("skill_ids")
            if "skill_ids" in payload
            else (existing or {}).get("skills")
            or (existing or {}).get("skill_ids")
            or ([context_skill] if context_skill else [])
        )
        skill_refs = normalize_skill_refs(raw_bindings)
        for item in skill_refs:
            item["skill_id"] = editor.validate_skill_name(item["skill_id"])
        skill_ids = [item["skill_id"] for item in skill_refs]
        if not skill_ids:
            raise SkillLabError("数据集至少需要关联一个 Skill")
        if not explicit_bindings and context_skill and context_skill not in skill_ids:
            skill_ids.append(context_skill)
            skill_refs.append({"skill_id": context_skill})
        requirements = _text_block(
            payload.get("requirements")
            if "requirements" in payload
            else (existing or {}).get("requirements")
        )
        trajectory_requirements = _text_block(
            payload.get("trajectory_requirements")
            if "trajectory_requirements" in payload
            else (existing or {}).get("trajectory_requirements")
        )
        if not requirements and not trajectory_requirements:
            raise SkillLabError("至少需要一条 Checklist")

        materials = list(existing.get("materials") or []) if existing else []
        decoded: list[tuple[str, bytes]] | None = None
        if files is not None:
            decoded = []
            total_bytes = 0
            seen: set[str] = set()
            for raw_file in files:
                rel_path = _normalize_material_path(raw_file.get("path"))
                if rel_path in seen:
                    raise SkillLabError(f"材料路径重复：{rel_path}")
                seen.add(rel_path)
                try:
                    data = base64.b64decode(
                        str(raw_file.get("content_b64") or ""),
                        validate=True,
                    )
                except (binascii.Error, ValueError) as exc:
                    raise SkillLabError(f"材料不是有效 Base64：{rel_path}") from exc
                if len(data) > _MAX_FILE_BYTES:
                    raise SkillLabError(f"单个材料不能超过 {_MAX_FILE_BYTES // (1024 * 1024)} MB：{rel_path}")
                total_bytes += len(data)
                if total_bytes > _MAX_DATASET_BYTES:
                    raise SkillLabError(
                        f"单个数据集材料合计不能超过 {_MAX_DATASET_BYTES // (1024 * 1024)} MB"
                    )
                decoded.append((rel_path, data))
        elif existing and not canonical_existing and materials:
            decoded = []
            for item in materials:
                if not isinstance(item, Mapping):
                    continue
                rel_path = _normalize_material_path(item.get("path"))
                data = self._bucket.get_object(
                    self._legacy_material_key(dataset_id, rel_path)
                ).read()
                decoded.append((rel_path, data))

        now = _utc_now_iso()
        source = (
            payload.get("source")
            if isinstance(payload.get("source"), dict)
            else (existing or {}).get("source")
            if isinstance((existing or {}).get("source"), dict)
            else {}
        )
        if str(source.get("kind") or "") == "evolution":
            source = {
                **source,
                "user_edited": True,
                "edited_at": now,
            }
        progressive = (
            payload.get("progressive_disclosure")
            if isinstance(payload.get("progressive_disclosure"), dict)
            else (existing or {}).get("progressive_disclosure")
            if isinstance((existing or {}).get("progressive_disclosure"), dict)
            else {}
        )
        dataset = {
            "dataset_id": dataset_id,
            "dataset_format": str(
                payload.get("dataset_format")
                or (existing or {}).get("dataset_format")
                or DATASET_SCHEMA_V2
            ),
            "skills": skill_refs,
            "skill_ids": skill_ids,
            "name": str(payload.get("name") or "").strip() or query.splitlines()[0][:80],
            "query": query,
            "requirements": requirements,
            "trajectory_requirements": trajectory_requirements,
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": max(
                    1,
                    int(progressive.get("batch_size") or 4),
                ),
                "stop_when": "all_checklist_items_satisfied",
            },
            "materials": materials,
            "source": source or {"kind": "manual"},
            "read_only": False,
            "enabled_for_evolution": bool(
                payload.get("enabled_for_evolution")
                if "enabled_for_evolution" in payload
                else (existing or {}).get("enabled_for_evolution", False)
            ),
            "created_at": str((existing or {}).get("created_at") or now),
            "updated_at": now,
        }
        available_paths = (
            [rel_path for rel_path, _data in decoded]
            if decoded is not None
            else self._available_material_paths(existing or dataset)
        )
        integrity = dataset_material_integrity(
            dataset,
            available_paths=available_paths,
        )
        if not integrity["complete"]:
            missing = "、".join(integrity["missing_paths"])
            raise SkillLabError(
                f"数据集引用了缺失材料：{missing}。请上传对应材料，"
                "或把必要内容直接内嵌到 Query 的“材料：”段落。"
            )
        if decoded is not None:
            dataset["materials"] = self._datasets.replace_materials(
                skill_name=context_skill or skill_ids[0],
                dataset_id=dataset_id,
                files=decoded,
            )
        saved = self._datasets.save_dataset(dataset)
        if existing and not canonical_existing:
            for obj in list(
                self._bucket.iter_objects(
                    prefix=self._legacy_dataset_prefix(dataset_id)
                )
            ):
                self._bucket.delete_object(obj.key)
        if context_skill and context_skill in saved.get("skill_ids", []):
            saved["skill_name"] = context_skill
        return self._present_dataset(saved)

    def load_dataset(
        self,
        dataset_id: str,
        *,
        skill_name: str = "",
    ) -> Optional[dict[str, Any]]:
        dataset = (
            self._datasets.load_dataset(
                skill_name=skill_name,
                dataset_id=dataset_id,
            )
            if skill_name
            else self._datasets.find_dataset(dataset_id)
        )
        if dataset:
            return self._present_dataset(dataset)
        dataset = self._read_json(self._legacy_dataset_key(dataset_id))
        if not dataset:
            return None
        if skill_name and str(dataset.get("skill_name") or "") != skill_name:
            return None
        return self._present_dataset(dataset)

    def list_datasets(self, *, skill_name: str = "") -> list[dict[str, Any]]:
        wanted = str(skill_name or "").strip()
        rows = [
            self._present_dataset(item)
            for item in self._datasets.list_datasets(skill_name=wanted)
        ]
        seen = {
            (
                str(item.get("skill_name") or ""),
                str(item.get("dataset_id") or ""),
            )
            for item in rows
        }
        for obj in self._bucket.iter_objects(prefix=f"{self._tenant_prefix()}datasets/"):
            if not obj.key.endswith("/metadata.json"):
                continue
            item = self._read_json(obj.key)
            if not item or (wanted and str(item.get("skill_name") or "") != wanted):
                continue
            key = (
                str(item.get("skill_name") or ""),
                str(item.get("dataset_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(self._present_dataset(item))
        rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return rows

    def material_payloads(self, dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
        dataset_id = _normalize_dataset_id(dataset.get("dataset_id"))
        skill_name = str(
            dataset.get("skill_name")
            or next(iter(dataset.get("skill_ids") or []), "")
        )
        skill_name = editor.validate_skill_name(skill_name) if skill_name else ""
        canonical = self._datasets.load_document(
            skill_name=skill_name,
            dataset_id=dataset_id,
        )
        payloads: list[dict[str, Any]] = []
        files = (
            self._datasets.read_materials(canonical)
            if canonical
            else [
                (
                    _normalize_material_path(item.get("path")),
                    self._bucket.get_object(
                        self._legacy_material_key(
                            dataset_id,
                            _normalize_material_path(item.get("path")),
                        )
                    ).read(),
                )
                for item in dataset.get("materials") or []
                if isinstance(item, Mapping) and item.get("path")
            ]
        )
        for rel_path, data in files:
            payloads.append(
                {
                    "path": rel_path,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return payloads

    def delete_dataset(self, dataset_id: str, *, skill_name: str = "") -> bool:
        dataset = self.load_dataset(dataset_id, skill_name=skill_name)
        if not dataset or bool(dataset.get("read_only")):
            return False
        owner = str(dataset.get("skill_name") or "")
        deleted = self._datasets.delete_dataset(
            skill_name=owner,
            dataset_id=dataset_id,
        )
        legacy_objects = list(
            self._bucket.iter_objects(
                prefix=self._legacy_dataset_prefix(dataset_id)
            )
        )
        for obj in legacy_objects:
            self._bucket.delete_object(obj.key)
        return deleted or bool(legacy_objects)

    def create_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _normalize_dataset_id(payload.get("run_id") or _run_id())
        now = _utc_now_iso()
        record = {
            **dict(payload),
            "run_id": run_id,
            "status": str(payload.get("status") or "running"),
            "created_at": str(payload.get("created_at") or now),
            "updated_at": now,
        }
        record.pop("result", None)
        self._write_json(self._run_key(run_id), record)
        return record

    def finish_run(
        self,
        run_id: str,
        *,
        result: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        record = self._read_json(self._run_key(run_id))
        if not record:
            raise SkillLabError(f"实验不存在：{run_id}")
        result_payload = dict(result)
        self._write_json(self._run_result_key(run_id), result_payload)
        record.update(
            {
                "status": status,
                "updated_at": _utc_now_iso(),
                "completed_at": _utc_now_iso(),
                "result_summary": {
                    "status": result_payload.get("status"),
                    "verdict": result_payload.get("verdict"),
                    "accepted": result_payload.get("accepted"),
                    "reason": result_payload.get("reason"),
                    "efficiency": result_payload.get("efficiency") or {},
                    "harness": result_payload.get("harness") or {},
                },
            }
        )
        self._write_json(self._run_key(run_id), record)
        return {**record, "result": result_payload}

    def load_run(self, run_id: str) -> Optional[dict[str, Any]]:
        record = self._read_json(self._run_key(run_id))
        if not record:
            return None
        result = self._read_json(self._run_result_key(run_id))
        return {**record, **({"result": result} if result is not None else {})}

    def list_runs(self, *, skill_name: str = "", limit: int = 100) -> list[dict[str, Any]]:
        wanted = str(skill_name or "").strip()
        rows: list[dict[str, Any]] = []
        for obj in self._bucket.iter_objects(prefix=f"{self._tenant_prefix()}runs/"):
            if not obj.key.endswith("/metadata.json"):
                continue
            item = self._read_json(obj.key)
            if not item or (wanted and str(item.get("skill_name") or "") != wanted):
                continue
            rows.append(item)
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows[: max(1, min(500, int(limit or 100)))]


def _job_skill_name(job: Mapping[str, Any]) -> str:
    candidate = job.get("candidate_skill")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    return str(
        job.get("skill_name")
        or job.get("candidate_skill_name")
        or candidate.get("name")
        or ""
    ).strip()


def evolution_datasets(config, *, skill_name: str) -> list[dict[str, Any]]:
    """Project historical evolution replay cases into read-only datasets."""
    wanted = editor.validate_skill_name(skill_name)
    try:
        from team_skills.candidates.store import ValidationStore

        jobs = ValidationStore.from_config(config).list_jobs()
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in reversed(jobs):
        if _job_skill_name(job) != wanted:
            continue
        job_id = str(job.get("job_id") or "")
        synthesized = [
            item
            for item in job.get("test_datasets") or []
            if isinstance(item, Mapping)
            and (
                bool(item.get("checks"))
                or bool(flatten_requirements(item.get("requirements")))
                or bool(
                    flatten_requirements(
                        item.get("trajectory_requirements")
                    )
                )
            )
        ]
        source_rows = synthesized or [
            case
            for case in job.get("replay_cases") or []
            if isinstance(case, Mapping)
            and (
                bool(case.get("checklist"))
                or bool(flatten_requirements(case.get("requirements")))
            )
        ]
        for index, case in enumerate(source_rows):
            if not isinstance(case, Mapping):
                continue
            try:
                canonical = normalize_case(
                    case,
                    default_case_id=str(
                        case.get("dataset_id")
                        or case.get("case_id")
                        or f"evolution-{index + 1}"
                    ),
                )
            except ValueError:
                continue
            view = legacy_case_view(canonical, text_requirements=True)
            instruction = canonical["query"]
            if not instruction:
                continue
            provenance = canonical.get("provenance") or {}
            source_session_ids = list(provenance.get("session_ids") or [])
            session_id = str(
                provenance.get("session_id")
                or (source_session_ids[0] if source_session_ids else "")
                or ""
            )
            requirements = flatten_requirements(view.get("requirements"))
            trajectory_requirements = flatten_requirements(view.get("trajectory_requirements"))
            dedup_key = canonical["case_id"] or "\0".join(
                [
                    session_id,
                    instruction,
                    *requirements,
                    *trajectory_requirements,
                ]
            )
            fingerprint = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            dataset_id = f"evo-{fingerprint[:20]}"
            if synthesized or requirements or trajectory_requirements:
                sections = {
                    "query": instruction,
                    "requirements": "\n".join(
                        f"{number}. {item}"
                        for number, item in enumerate(
                            requirements,
                            start=1,
                        )
                    ),
                    "trajectory_requirements": "\n".join(
                        f"{number}. {item}"
                        for number, item in enumerate(
                            trajectory_requirements,
                            start=1,
                        )
                    ),
                }
            else:
                try:
                    sections = parse_dataset_markdown(instruction)
                except SkillLabError:
                    sections = {
                        "query": instruction,
                        "requirements": (
                            json.dumps(
                                case.get("gold"),
                                ensure_ascii=False,
                                indent=2,
                            )
                            if isinstance(case.get("gold"), Mapping)
                            and case.get("gold")
                            else ""
                        ),
                        "trajectory_requirements": "\n".join(
                            f"- {item}"
                            for item in case.get("target_dimensions") or []
                        ),
                    }
            window = str(provenance.get("evidence_window") or "historical")
            materials = [
                {
                    key: item.get(key)
                    for key in ("path", "size", "sha256")
                    if item.get(key) is not None
                }
                for item in canonical.get("materials") or []
                if isinstance(item, Mapping) and item.get("path")
            ]
            replay = canonical.get("replay") if isinstance(canonical.get("replay"), Mapping) else {}
            bound_skill_ids = list(canonical.get("skill_ids") or [wanted])
            dataset = {
                "dataset_id": canonical["case_id"] or dataset_id,
                "dataset_format": DATASET_SCHEMA_V2,
                "skill_name": wanted,
                "skills": [
                    {"skill_id": skill_id}
                    for skill_id in bound_skill_ids
                ],
                "skill_ids": bound_skill_ids,
                "name": str(
                    canonical.get("name")
                    or case.get("case_id")
                    or session_id
                    or f"{window}-{index + 1}"
                ),
                **sections,
                "materials": materials,
                "progressive_disclosure": (
                    dict(replay.get("progressive_disclosure"))
                    if isinstance(
                        replay.get("progressive_disclosure"),
                        Mapping,
                    )
                    else {
                        "enabled": True,
                        "initial_visibility": "query_only",
                        "batch_size": 4,
                        "stop_when": "all_checklist_items_satisfied",
                    }
                ),
                "source": {
                    "kind": "evolution",
                    "job_id": job_id,
                    "session_id": session_id,
                    "source_session_ids": source_session_ids
                    or ([session_id] if session_id else []),
                    "turn_num": provenance.get("turn_num"),
                    "evidence_window": window,
                },
                "read_only": True,
                "enabled_for_evolution": False,
                "created_at": str(job.get("created_at") or ""),
                "updated_at": str(job.get("updated_at") or job.get("created_at") or ""),
            }
            dataset["material_integrity"] = dataset_material_integrity(
                dataset,
                available_paths=[],
            )
            rows.append(
                {**dataset, "dataset_markdown": render_dataset_markdown(dataset)}
            )
    return rows[:200]


def resolve_dataset(
    config,
    store: SkillLabStore,
    *,
    skill_name: str,
    dataset_id: str,
) -> Optional[dict[str, Any]]:
    persisted = store.load_dataset(dataset_id, skill_name=skill_name)
    if persisted:
        return persisted
    return next(
        (
            item
            for item in evolution_datasets(config, skill_name=skill_name)
            if item.get("dataset_id") == dataset_id
        ),
        None,
    )


def parse_skill_markdown(raw: str) -> dict[str, Any]:
    """Parse an in-memory SKILL.md draft without writing temporary files."""
    text = str(raw or "")
    split = frontmatter._split_frontmatter(text)  # shared wire-format parser
    if split is None:
        raise SkillLabError("SKILL.md 缺少 YAML frontmatter")
    fm_text, body = split
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise SkillLabError(f"SKILL.md frontmatter 不是有效 YAML：{exc}") from exc
    if not isinstance(fm, dict):
        raise SkillLabError("SKILL.md frontmatter 必须是对象")
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not name or not description:
        raise SkillLabError("SKILL.md 必须包含 name 和 description")
    extra = {key: value for key, value in fm.items() if key not in _CORE_SKILL_KEYS}
    payload: dict[str, Any] = {
        "name": editor.validate_skill_name(name),
        "description": description,
        "category": frontmatter.resolve_category(fm) or "general",
        "content": body,
    }
    if extra:
        payload["extra_frontmatter"] = extra
    return payload


def prepare_experiment_job(
    *,
    load_bundle: Callable[[str], Mapping[str, bytes]],
    skill_name: str,
    candidate_skill_md: str,
    dataset: Mapping[str, Any],
    materials: list[Mapping[str, Any]],
    run_id: str,
    candidate_files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a True Replay job from the current skill, draft, and dataset.

    *load_bundle* resolves a skill name to its ``{relative_path: bytes}`` bundle
    (the team skill library, so experiments replay exactly what Agents receive);
    it must raise :class:`FileNotFoundError` for an unknown skill.
    """
    name = editor.validate_skill_name(skill_name)
    current_bundle = _load_skill_bundle(load_bundle, name)
    current_payload = parse_skill_markdown(
        current_bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
    )
    candidate_payload = parse_skill_markdown(candidate_skill_md)
    if candidate_payload["name"] != name:
        raise SkillLabError("实验草稿的 frontmatter name 必须与所选 Skill 一致")

    current_skill = attach_bundle_payload(current_payload, current_bundle)
    candidate_bundle = dict(current_bundle)
    if candidate_files is not None:
        if not isinstance(candidate_files, Mapping) or len(candidate_files) > 100:
            raise SkillLabError("candidate_files 必须为至多 100 个文本文件的映射")
        total_bytes = 0
        for path, text in candidate_files.items():
            if (
                not isinstance(path, str) or not path or path.startswith("/")
                or "\\" in path or ":" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path == "SKILL.md"
            ):
                raise SkillLabError("Candidate 文件必须使用安全的相对路径；SKILL.md 使用独立字段")
            if not isinstance(text, str):
                raise SkillLabError("Candidate 文件内容必须为文本")
            data = text.encode("utf-8")
            total_bytes += len(data)
            if len(data) > 2 * 1024 * 1024 or total_bytes > 16 * 1024 * 1024:
                raise SkillLabError("Candidate 文件超过大小限制")
            candidate_bundle[path] = data
    candidate_bundle["SKILL.md"] = candidate_skill_md.encode("utf-8")
    candidate_skill = attach_bundle_payload(candidate_payload, candidate_bundle)
    skill_refs = normalize_skill_refs(
        dataset.get("skills")
        or dataset.get("skill_ids")
        or [name]
    )
    skill_ids = [
        editor.validate_skill_name(item["skill_id"])
        for item in skill_refs
    ]
    if name not in skill_ids:
        raise SkillLabError("当前实验 Skill 必须包含在数据集关联 Skills 中")
    current_skills = []
    candidate_skills = []
    for bound_skill_name in skill_ids:
        if bound_skill_name == name:
            baseline_member = current_skill
            candidate_member = candidate_skill
        else:
            bound_bundle = _load_skill_bundle(load_bundle, bound_skill_name)
            bound_payload = parse_skill_markdown(
                bound_bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
            )
            baseline_member = attach_bundle_payload(bound_payload, bound_bundle)
            candidate_member = baseline_member
        current_skills.append(baseline_member)
        candidate_skills.append(candidate_member)
    current_treatment = (
        current_skills[0]
        if len(current_skills) == 1
        else {"kind": "skill_set", "skills": current_skills}
    )
    candidate_treatment = (
        candidate_skills[0]
        if len(candidate_skills) == 1
        else {"kind": "skill_set", "skills": candidate_skills}
    )
    has_nested_tasks = isinstance(dataset.get("cases"), list)
    raw_tasks = (
        [
            task
            for task in dataset.get("cases") or []
            if isinstance(task, Mapping)
        ]
        if isinstance(dataset.get("cases"), list)
        else []
    ) or [dataset]
    replay_cases = []
    replay_session_ids: list[str] = []
    for raw_task in raw_tasks:
        task = legacy_case_view(
            normalize_case(
                raw_task,
                default_case_id=str(
                    raw_task.get("case_id")
                    or raw_task.get("dataset_id")
                    or dataset.get("dataset_id")
                    or ""
                ),
            )
        )
        task_skill_ids = [
            editor.validate_skill_name(item)
            for item in (
                task.get("skill_ids")
                or skill_ids
            )
        ]
        if name not in task_skill_ids:
            continue
        source = task.get("source") if isinstance(task.get("source"), Mapping) else {}
        source_session_ids = [
            str(item or "").strip()
            for item in (
                source.get("source_session_ids")
                or source.get("session_ids")
                or []
            )
            if str(item or "").strip()
        ]
        session_id = str(
            source.get("session_id")
            or (source_session_ids[0] if source_session_ids else "")
            or ""
        )
        if session_id and session_id not in replay_session_ids:
            replay_session_ids.append(session_id)
        requirements = flatten_requirements(task.get("requirements"))
        task_trajectory = flatten_requirements(
            task.get("trajectory_requirements")
        )
        task_material_paths = {
            str(item.get("path") or "")
            for item in task.get("materials") or []
            if isinstance(item, Mapping) and item.get("path")
        }
        task_materials = [
            dict(item)
            for item in materials
            if (not has_nested_tasks and not task_material_paths)
            or str(item.get("path") or "") in task_material_paths
        ]
        disclosure = (
            task.get("progressive_disclosure")
            if isinstance(task.get("progressive_disclosure"), Mapping)
            else {}
        )
        replay_cases.append({
            "case_id": str(
                task.get("case_id")
                or task.get("dataset_id")
                or dataset.get("dataset_id")
                or ""
            ),
            "dataset_id": str(dataset.get("dataset_id") or ""),
            "skill_ids": task_skill_ids,
            "session_id": session_id,
            "turn_num": source.get("turn_num") or 1,
            "instruction": compose_experiment_instruction(task),
            "query": compose_experiment_instruction(task),
            "requirements": requirements,
            "trajectory_requirements": task_trajectory,
            "checklist": checklist_items(requirements, task_trajectory),
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": max(
                    1,
                    int(disclosure.get("batch_size") or 4),
                ),
                "stop_when": "all_checklist_items_satisfied",
            },
            "materials": task_materials,
            "evidence_window": str(source.get("evidence_window") or "lab"),
            "dataset_format": DATASET_SCHEMA_V2,
        })
    if not replay_cases:
        raise SkillLabError("数据集中没有引用当前实验 Skill 的任务")
    return {
        "job_id": run_id,
        "skill_name": name,
        "candidate_skill_name": name,
        "proposed_action": "experiment",
        "rationale": "Developer-triggered Skill Lab True Replay experiment.",
        "current_skill": current_treatment,
        "candidate_skill": candidate_treatment,
        "skill_ids": skill_ids,
        "changed_skill_ids": [name],
        "replay_cases": replay_cases,
        "session_ids": replay_session_ids,
        "include_full_trace": True,
        "source": {
            "kind": "skill_lab",
            "dataset_id": str(dataset.get("dataset_id") or ""),
            "dataset_source": dict(source),
            "skill_ids": skill_ids,
            "changed_skill_ids": [name],
        },
    }
