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
from typing import Any, Mapping, Optional

import yaml

from .dataset_store import (
    SkillDatasetStore,
    dataset_material_integrity,
)
from .dataset_synthesizer import checklist_items, flatten_requirements
from .skills import editor, frontmatter
from .skills.bundle import attach_bundle_payload, read_skill_bundle
from .storage import InMemoryObjectStore, is_not_found_error

logger = logging.getLogger(__name__)


_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_DATASET_BYTES = 80 * 1024 * 1024
_CORE_SKILL_KEYS = {"name", "description", "category"}


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
        self._datasets = SkillDatasetStore(bucket)

    # Process-lifetime fallback store shared across from_config() calls, used
    # only when no OpenViking backend is configured. Data is ephemeral (in
    # memory) — configure cloud or local OpenViking to persist skill-lab state.
    _fallback_bucket: "InMemoryObjectStore | None" = None

    @classmethod
    def from_config(cls, config) -> "SkillLabStore":
        from .skills.hub import SkillHub

        hub = SkillHub.object_storage_from_config(config)
        if hub is not None:
            return cls(hub._bucket)
        logger.warning(
            "[SkillLabStore] no OpenViking backend configured; using an "
            "in-memory fallback store. Skill-lab data will not persist across "
            "restarts. Configure cloud or local OpenViking to persist."
        )
        if cls._fallback_bucket is None:
            cls._fallback_bucket = InMemoryObjectStore("skill_lab")
        return cls(cls._fallback_bucket)

    @staticmethod
    def make_run_id() -> str:
        return _run_id()

    def _legacy_dataset_key(self, dataset_id: str) -> str:
        return f"skill_lab/datasets/{_normalize_dataset_id(dataset_id)}/metadata.json"

    def _legacy_dataset_prefix(self, dataset_id: str) -> str:
        return f"skill_lab/datasets/{_normalize_dataset_id(dataset_id)}/"

    def _legacy_material_key(self, dataset_id: str, rel_path: str) -> str:
        return (
            f"{self._legacy_dataset_prefix(dataset_id)}materials/"
            f"{_normalize_material_path(rel_path)}"
        )

    def _run_key(self, run_id: str) -> str:
        return f"skill_lab/runs/{_normalize_dataset_id(run_id)}/metadata.json"

    def _run_result_key(self, run_id: str) -> str:
        return f"skill_lab/runs/{_normalize_dataset_id(run_id)}/result.json"

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
        canonical = self._datasets.load_dataset(
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
        skill_name = editor.validate_skill_name(str(payload.get("skill_name") or ""))
        query = _text_block(payload.get("query"))
        if not query:
            raise SkillLabError("query 不能为空")
        dataset_id = (
            _normalize_dataset_id(payload.get("dataset_id"))
            if payload.get("dataset_id")
            else _dataset_id()
        )
        canonical_existing = self._datasets.load_dataset(
            skill_name=skill_name,
            dataset_id=dataset_id,
        )
        existing = (
            canonical_existing
            or self._datasets.find_dataset(dataset_id)
            or self.load_dataset(
                dataset_id,
                skill_name=skill_name,
            )
        )
        if existing and str(existing.get("skill_name") or "") != skill_name:
            raise SkillLabError("不能把已有数据集移动到另一个 skill")

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
                or "teamEvolver-skill-dataset-v1"
            ),
            "skill_name": skill_name,
            "name": str(payload.get("name") or "").strip() or query.splitlines()[0][:80],
            "query": query,
            "requirements": _text_block(payload.get("requirements")),
            "trajectory_requirements": _text_block(
                payload.get("trajectory_requirements")
            ),
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
                skill_name=skill_name,
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
        for obj in self._bucket.iter_objects(prefix="skill_lab/datasets/"):
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
        skill_name = editor.validate_skill_name(
            str(dataset.get("skill_name") or "")
        )
        canonical = self._datasets.load_dataset(
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
        for obj in self._bucket.iter_objects(prefix="skill_lab/runs/"):
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
        from .validation.store import ValidationStore

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
                bool(flatten_requirements(item.get("requirements")))
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
            instruction = str(
                case.get("query") or case.get("instruction") or ""
            ).strip()
            if not instruction:
                continue
            source_session_ids = [
                str(item or "").strip()
                for item in case.get("source_session_ids") or []
                if str(item or "").strip()
            ]
            session_id = str(
                case.get("session_id")
                or (source_session_ids[0] if source_session_ids else "")
                or ""
            )
            requirements = flatten_requirements(case.get("requirements"))
            trajectory_requirements = flatten_requirements(
                case.get("trajectory_requirements")
            )
            dedup_key = str(case.get("dataset_id") or "").strip() or "\0".join(
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
            if synthesized:
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
            window = str(case.get("evidence_window") or "historical")
            materials = [
                {
                    key: item.get(key)
                    for key in ("path", "size", "sha256")
                    if item.get(key) is not None
                }
                for item in case.get("materials") or []
                if isinstance(item, Mapping) and item.get("path")
            ]
            dataset = {
                "dataset_id": str(case.get("dataset_id") or dataset_id),
                "dataset_format": str(
                    case.get("dataset_format")
                    or "teamEvolver-progressive-test-v1"
                ),
                "skill_name": wanted,
                "name": str(
                    case.get("name")
                    or case.get("case_id")
                    or session_id
                    or f"{window}-{index + 1}"
                ),
                **sections,
                "materials": materials,
                "progressive_disclosure": (
                    dict(case.get("progressive_disclosure"))
                    if isinstance(
                        case.get("progressive_disclosure"),
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
                    "turn_num": case.get("turn_num"),
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
    skills_dir: str,
    skill_name: str,
    candidate_skill_md: str,
    dataset: Mapping[str, Any],
    materials: list[Mapping[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    """Build a True Replay job from the current skill, draft, and dataset."""
    name = editor.validate_skill_name(skill_name)
    current_detail = editor.get_skill(skills_dir, name)
    current_dir = editor.find_skill_dir(skills_dir, name)
    if not current_dir:
        raise SkillLabError(f"Skill 不存在：{name}")
    current_payload = parse_skill_markdown(str(current_detail.get("skill_md") or ""))
    candidate_payload = parse_skill_markdown(candidate_skill_md)
    if candidate_payload["name"] != name:
        raise SkillLabError("实验草稿的 frontmatter name 必须与所选 Skill 一致")

    current_bundle = read_skill_bundle(current_dir)
    current_skill = attach_bundle_payload(current_payload, current_bundle)
    candidate_bundle = dict(current_bundle)
    candidate_bundle["SKILL.md"] = candidate_skill_md.encode("utf-8")
    candidate_skill = attach_bundle_payload(candidate_payload, candidate_bundle)
    source = dataset.get("source") if isinstance(dataset.get("source"), Mapping) else {}
    source_session_ids = [
        str(item or "").strip()
        for item in source.get("source_session_ids") or []
        if str(item or "").strip()
    ]
    session_id = str(
        source.get("session_id")
        or (source_session_ids[0] if source_session_ids else "")
        or ""
    )
    requirements = flatten_requirements(dataset.get("requirements"))
    trajectory_requirements = flatten_requirements(
        dataset.get("trajectory_requirements")
    )
    replay_case = {
        "case_id": str(dataset.get("dataset_id") or ""),
        "dataset_id": str(dataset.get("dataset_id") or ""),
        "session_id": session_id,
        "turn_num": source.get("turn_num") or 1,
        "instruction": compose_experiment_instruction(dataset),
        "query": compose_experiment_instruction(dataset),
        "requirements": requirements,
        "trajectory_requirements": trajectory_requirements,
        "checklist": checklist_items(
            requirements,
            trajectory_requirements,
        ),
        "progressive_disclosure": {
            "enabled": True,
            "initial_visibility": "query_only",
            "batch_size": max(
                1,
                int(
                    (
                        dataset.get("progressive_disclosure")
                        if isinstance(
                            dataset.get("progressive_disclosure"),
                            Mapping,
                        )
                        else {}
                    ).get("batch_size")
                    or 4
                ),
            ),
            "stop_when": "all_checklist_items_satisfied",
        },
        "materials": [dict(item) for item in materials],
        "evidence_window": str(source.get("evidence_window") or "lab"),
        "dataset_format": str(
            dataset.get("dataset_format")
            or "teamEvolver-skill-dataset-v1"
        ),
    }
    return {
        "job_id": run_id,
        "skill_name": name,
        "candidate_skill_name": name,
        "proposed_action": "experiment",
        "rationale": "Developer-triggered Skill Lab True Replay experiment.",
        "current_skill": current_skill,
        "candidate_skill": candidate_skill,
        "replay_cases": [replay_case],
        "session_ids": [session_id] if session_id else [],
        "include_full_trace": True,
        "source": {
            "kind": "skill_lab",
            "dataset_id": str(dataset.get("dataset_id") or ""),
            "dataset_source": dict(source),
        },
    }
