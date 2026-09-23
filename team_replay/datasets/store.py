"""Canonical object-store repository for Skill-owned datasets."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

from teamEvolver.storage import is_not_found_error

from .schema import (
    DATASET_SCHEMA_V2,
    document_skill_ids,
    legacy_case_view,
    make_document,
    normalize_document,
    normalize_skill_refs,
    validate_document,
)


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_FILE_EXTENSION_RE = re.compile(
    r"\.(?:"
    r"csv|tsv|json|jsonl|ya?ml|md|txt|pdf|docx?|xlsx?|pptx?|"
    r"html?|zip|tar|gz|tgz|png|jpe?g|gif|webp|svg|mp[34]|wav"
    r")$",
    re.IGNORECASE,
)
_INLINE_MATERIAL_RE = re.compile(
    r"(?ims)(?:^|\n)\s*(?:材料|素材|输入数据|参考资料|reference data)"
    r"\s*[：:]\s*\S"
)
_INPUT_MATERIAL_HINT_RE = re.compile(
    r"(?is)(?:"
    r"(?:根据|基于|读取|查看|分析|处理|使用|解压).{0,18}"
    r"(?:材料|素材|附件|文件|压缩包)"
    r"|输入材料|源文件|附件中|工作目录.{0,24}\.(?:zip|csv|json|xlsx?|pdf)"
    r"|(?:read|reads|inspect|analy[sz]e|extract|use|check|verify|open|load).{0,18}"
    r"(?:material|attachment|input file|archive|package|config|data|file)"
    r")"
)
_PATH_TOKEN_RE = re.compile(r"[^\s,;，；、：:。！？!?（）()\[\]{}<>]+")
_INPUT_MARKERS = (
    "读取",
    "查看",
    "分析",
    "解压",
    "输入",
    "材料",
    "素材",
    "附件",
    "源文件",
    "目录里有",
    "基于",
    "根据",
    "input",
    "source",
    "read",
    "reads",
    "inspect",
    "check",
    "verify",
    "open",
    "load",
    "读取文件",
    "参考",
)
_OUTPUT_MARKERS = (
    "保存到",
    "写入",
    "写到",
    "输出到",
    "产出到",
    "生成到",
    "报告到",
    "输出目录",
    "保存路径",
    "目标路径",
    "output_path",
    "output directory",
    "write to",
    "save to",
    "writes",
    "saves",
    "generates",
    "creates",
    "output",
    "写入文件",
)
_SKILL_BUNDLE_PREFIXES = ("references/", "assets/", "scripts/")


class SkillDatasetStoreError(ValueError):
    """Raised when a dataset key is malformed or unsafe."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_dataset_id(value: Any) -> str:
    dataset_id = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(dataset_id):
        raise SkillDatasetStoreError(
            "dataset_id must contain only letters, digits, '.', '-' or '_'"
        )
    return dataset_id


def normalize_skill_name(value: Any) -> str:
    skill_name = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(skill_name):
        raise SkillDatasetStoreError(
            "skill_name must contain only letters, digits, '.', '-' or '_'"
        )
    return skill_name


def dataset_skill_refs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = normalize_skill_refs(payload.get("skills"))
    if not refs:
        refs = normalize_skill_refs(payload.get("skill_ids"))
    if not refs and payload.get("skill_name"):
        refs = normalize_skill_refs([payload.get("skill_name")])
    if not refs and isinstance(payload.get("subject"), Mapping):
        refs = normalize_skill_refs([payload["subject"]])
    return refs


def dataset_material_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_cases = payload.get("cases")
    values = (
        [
            material
            for case in raw_cases
            if isinstance(case, Mapping)
            for material in case.get("materials") or []
        ]
        if isinstance(raw_cases, list)
        else payload.get("materials") or []
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        path = normalize_material_path(item.get("path"))
        if path in seen:
            continue
        seen.add(path)
        result.append(dict(item))
    return result


def normalize_material_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if (
        not raw
        or raw.startswith("/")
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SkillDatasetStoreError(f"unsafe material path: {value!r}")
    return "/".join(parts)


def _path_tokens(text: str) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(str(text or "")):
        token = match.group(0).strip("`'\"“”‘’")
        token = token.rstrip("，。；;：:、!?！？")
        if token.lower().startswith("output_path="):
            continue
        for quote in ("“", "‘", "`", "'", '"'):
            if quote in token:
                token = token.rsplit(quote, 1)[-1]
        if not token or token.startswith(("http://", "https://")):
            continue
        if re.fullmatch(r"\.[A-Za-z0-9]+", token):
            continue
        looks_pathy = (
            token.startswith("/")
            or token.endswith("/")
            or bool(_FILE_EXTENSION_RE.search(token))
        )
        if not looks_pathy or token in seen:
            continue
        seen.add(token)
        hits.append((token, match.start()))
    return hits


def _nearest_marker(context: str, markers: tuple[str, ...]) -> int:
    lowered = context.lower()
    return max((lowered.rfind(marker) for marker in markers), default=-1)


def dataset_material_requirements(
    dataset: Mapping[str, Any],
) -> dict[str, Any]:
    """Identify inline material and external input paths in a dataset."""
    if (
        dataset.get("schema_version") == DATASET_SCHEMA_V2
        and isinstance(dataset.get("cases"), list)
        and dataset["cases"]
    ):
        dataset = dataset["cases"][0]
    query = str(dataset.get("query") or "")
    inline = bool(_INLINE_MATERIAL_RE.search(query))
    required_paths: list[str] = []
    for path, offset in _path_tokens(query):
        relative = path.lstrip("/")
        if relative.startswith(_SKILL_BUNDLE_PREFIXES):
            continue
        context = query[max(0, offset - 64) : offset]
        input_pos = _nearest_marker(context, _INPUT_MARKERS)
        output_pos = _nearest_marker(context, _OUTPUT_MARKERS)
        if input_pos < 0:
            continue
        if output_pos >= 0 and output_pos >= input_pos:
            continue
        if path not in required_paths:
            required_paths.append(path)
    generic_reference = bool(
        _INPUT_MATERIAL_HINT_RE.search(query)
        and not inline
        and not required_paths
    )
    return {
        "inline": inline,
        "required_paths": required_paths,
        "generic_reference": generic_reference,
    }


def _material_matches(expected: str, available: str) -> bool:
    wanted = str(expected or "").strip().replace("\\", "/").rstrip("/")
    actual = str(available or "").strip().replace("\\", "/").rstrip("/")
    if not wanted or not actual:
        return False
    if wanted == actual or wanted.endswith(f"/{actual}"):
        return True
    wanted_name = PurePosixPath(wanted).name
    actual_name = PurePosixPath(actual).name
    if _FILE_EXTENSION_RE.search(wanted):
        return wanted_name == actual_name
    return (
        actual == wanted_name
        or actual.startswith(f"{wanted_name}/")
        or f"/{wanted_name}/" in f"/{actual}/"
    )


def dataset_material_integrity(
    dataset: Mapping[str, Any],
    *,
    available_paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Return a deterministic material-completeness report."""
    material_source = dataset
    if (
        dataset.get("schema_version") == DATASET_SCHEMA_V2
        and isinstance(dataset.get("cases"), list)
        and dataset["cases"]
    ):
        material_source = dataset["cases"][0]
    requirements = dataset_material_requirements(material_source)
    available = list(
        dict.fromkeys(
            str(path or "").strip().replace("\\", "/")
            for path in (
                available_paths
                if available_paths is not None
                else [
                    item.get("path")
                    for item in material_source.get("materials") or []
                    if isinstance(item, Mapping)
                    and item.get("path")
                    and item.get("available", True)
                ]
            )
            if str(path or "").strip()
        )
    )
    missing = [
        expected
        for expected in requirements["required_paths"]
        if not any(
            _material_matches(expected, candidate)
            for candidate in available
        )
    ]
    if requirements["generic_reference"] and not available:
        missing.append("未提供输入材料")
    inline = bool(requirements["inline"])
    if missing:
        status = "missing"
        mode = "mixed" if inline else "external"
        message = "缺少数据集引用的输入材料"
    elif available and inline:
        status, mode, message = "complete", "mixed", "内嵌与上传材料齐全"
    elif available:
        status, mode, message = "complete", "uploaded", "上传材料齐全"
    elif inline:
        status, mode, message = "complete", "inline", "材料已内嵌在 Query"
    else:
        status, mode, message = "not_required", "none", "数据集不依赖外部材料"
    return {
        "status": status,
        "mode": mode,
        "complete": not missing,
        "inline": inline,
        "required_paths": requirements["required_paths"],
        "available_paths": available,
        "missing_paths": missing,
        "message": message,
    }


class SkillDatasetStore:
    """Persist peer-Skill datasets under ``skill_datasets/by-id/<dataset>``."""

    def __init__(self, bucket: Any, *, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")

    def root_prefix(self) -> str:
        return f"{self._prefix}skill_datasets/"

    def dataset_prefix(self, skill_name: str, dataset_id: str) -> str:
        del skill_name
        return (
            f"{self.root_prefix()}by-id/"
            f"{normalize_dataset_id(dataset_id)}/"
        )

    def legacy_dataset_prefix(self, skill_name: str, dataset_id: str) -> str:
        return (
            f"{self.root_prefix()}{normalize_skill_name(skill_name)}/"
            f"{normalize_dataset_id(dataset_id)}/"
        )

    def dataset_key(self, skill_name: str, dataset_id: str) -> str:
        return f"{self.dataset_prefix(skill_name, dataset_id)}metadata.json"

    def legacy_dataset_key(self, skill_name: str, dataset_id: str) -> str:
        return f"{self.legacy_dataset_prefix(skill_name, dataset_id)}metadata.json"

    def material_key(
        self,
        skill_name: str,
        dataset_id: str,
        rel_path: str,
    ) -> str:
        return (
            f"{self.dataset_prefix(skill_name, dataset_id)}materials/"
            f"{normalize_material_path(rel_path)}"
        )

    def legacy_material_key(
        self,
        skill_name: str,
        dataset_id: str,
        rel_path: str,
    ) -> str:
        return (
            f"{self.legacy_dataset_prefix(skill_name, dataset_id)}materials/"
            f"{normalize_material_path(rel_path)}"
        )

    def _read_json(self, key: str) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(
                self._bucket.get_object(key).read().decode("utf-8")
            )
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise
        return value if isinstance(value, dict) else None

    @staticmethod
    def _document_view(
        document: Mapping[str, Any],
        *,
        selected_skill_id: str = "",
    ) -> dict[str, Any]:
        cases = [
            legacy_case_view(case, document=document)
            for case in document.get("cases") or []
            if isinstance(case, Mapping)
        ]
        result = cases[0] if cases else {}
        result["dataset_id"] = str(document.get("dataset_id") or "")
        result["name"] = str(document.get("name") or result.get("name") or "")
        result["description"] = str(document.get("description") or "")
        result["created_at"] = str(document.get("created_at") or "")
        result["updated_at"] = str(document.get("updated_at") or "")
        if document.get("source"):
            result["source"] = dict(document["source"])
        result["cases"] = cases
        result["task_count"] = len(cases)
        result["skill_ids"] = document_skill_ids(document)
        result["skills"] = normalize_skill_refs(document.get("skills"))
        if selected_skill_id:
            result["skill_name"] = selected_skill_id
        return result

    def save_dataset(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        dataset_id = normalize_dataset_id(payload.get("dataset_id"))
        refs = dataset_skill_refs(payload)
        lookup_skill = refs[0]["skill_id"] if refs else ""
        existing_document = self.load_document(
            skill_name="",
            dataset_id=dataset_id,
        )
        if existing_document is None and lookup_skill:
            existing_document = self.load_document(
                skill_name=lookup_skill,
                dataset_id=dataset_id,
            )
        if not refs and existing_document:
            refs = normalize_skill_refs(existing_document.get("skills"))
        if not refs:
            raise SkillDatasetStoreError("at least one skill_id is required")
        for ref in refs:
            ref["skill_id"] = normalize_skill_name(ref["skill_id"])
        now = utc_now_iso()
        normalized = normalize_document(
            payload,
            default_dataset_id=dataset_id,
            default_name=str(payload.get("name") or ""),
            default_subject={"kind": "skill", "id": refs[0]["skill_id"]},
        )
        cases = normalized["cases"]
        document = make_document(
            dataset_id=dataset_id,
            name=str(normalized.get("name") or cases[0].get("name") or ""),
            description=str(normalized.get("description") or ""),
            skills=refs,
            source=normalized.get("source") or {},
            cases=cases,
            metadata=normalized.get("metadata") or {},
            created_at=str(
                payload.get("created_at")
                or (existing_document or {}).get("created_at")
                or now
            ),
            updated_at=str(payload.get("updated_at") or now),
        )
        errors = validate_document(document)
        if errors:
            raise SkillDatasetStoreError("invalid Test Dataset: " + "; ".join(errors))
        self._bucket.put_object(
            self.dataset_key("", dataset_id),
            json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        result = self._document_view(document)
        return result

    def load_document(
        self,
        *,
        skill_name: str,
        dataset_id: str,
    ) -> Optional[dict[str, Any]]:
        wanted = normalize_skill_name(skill_name) if skill_name else ""
        raw = self._read_json(self.dataset_key("", dataset_id))
        if raw is None and wanted:
            raw = self._read_json(self.legacy_dataset_key(wanted, dataset_id))
        if raw is None:
            return None
        document = normalize_document(
            raw,
            default_dataset_id=dataset_id,
            default_name=str(raw.get("name") or ""),
            default_subject={"kind": "skill", "id": wanted} if wanted else None,
        )
        if wanted and wanted not in document_skill_ids(document):
            return None
        return document

    def load_dataset(
        self,
        *,
        skill_name: str,
        dataset_id: str,
    ) -> Optional[dict[str, Any]]:
        document = self.load_document(
            skill_name=skill_name,
            dataset_id=dataset_id,
        )
        if document is None:
            return None
        return self._document_view(
            document,
            selected_skill_id=(
                normalize_skill_name(skill_name)
                if skill_name
                else ""
            ),
        )

    def find_dataset(self, dataset_id: str) -> Optional[dict[str, Any]]:
        wanted = normalize_dataset_id(dataset_id)
        for dataset in self.list_datasets():
            if str(dataset.get("dataset_id") or "") == wanted:
                return dataset
        return None

    def list_datasets(self, *, skill_name: str = "") -> list[dict[str, Any]]:
        wanted = normalize_skill_name(skill_name) if skill_name else ""
        prefixes = [f"{self.root_prefix()}by-id/"]
        if wanted:
            prefixes.append(f"{self.root_prefix()}{wanted}/")
        else:
            prefixes.append(self.root_prefix())
        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_ids: set[str] = set()
        for prefix in prefixes:
            for obj in self._bucket.iter_objects(prefix=prefix):
                if obj.key in seen_keys or not obj.key.endswith("/metadata.json"):
                    continue
                seen_keys.add(obj.key)
                item = self._read_json(obj.key)
                if not item:
                    continue
                try:
                    document = normalize_document(
                        item,
                        default_dataset_id=str(item.get("dataset_id") or ""),
                        default_name=str(item.get("name") or ""),
                        default_subject=(
                            {"kind": "skill", "id": wanted}
                            if wanted and "/by-id/" not in obj.key
                            else None
                        ),
                    )
                except ValueError:
                    continue
                if wanted and wanted not in document_skill_ids(document):
                    continue
                dataset_id = str(document.get("dataset_id") or "")
                if dataset_id in seen_ids:
                    continue
                seen_ids.add(dataset_id)
                view = self._document_view(
                    document,
                    selected_skill_id=wanted,
                )
                rows.append(view)
        rows.sort(
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
            reverse=True,
        )
        return rows

    def replace_materials(
        self,
        *,
        skill_name: str,
        dataset_id: str,
        files: list[tuple[str, bytes]],
    ) -> list[dict[str, Any]]:
        import hashlib

        materials_prefix = (
            f"{self.dataset_prefix(skill_name, dataset_id)}materials/"
        )
        prefixes = [materials_prefix]
        if skill_name:
            prefixes.append(
                f"{self.legacy_dataset_prefix(skill_name, dataset_id)}materials/"
            )
        for obj in [
            item
            for prefix in prefixes
            for item in self._bucket.iter_objects(prefix=prefix)
        ]:
            self._bucket.delete_object(obj.key)
        materials: list[dict[str, Any]] = []
        for raw_path, data in files:
            rel_path = normalize_material_path(raw_path)
            self._bucket.put_object(
                self.material_key(skill_name, dataset_id, rel_path),
                data,
            )
            materials.append(
                {
                    "path": rel_path,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return materials

    def read_materials(
        self,
        dataset: Mapping[str, Any],
    ) -> list[tuple[str, bytes]]:
        skill_ids = [
            str(item or "")
            for item in dataset.get("skill_ids") or []
            if str(item or "")
        ]
        skill_name = str(dataset.get("skill_name") or next(iter(skill_ids), ""))
        dataset_id = normalize_dataset_id(dataset.get("dataset_id"))
        files: list[tuple[str, bytes]] = []
        for item in dataset_material_entries(dataset):
            if not isinstance(item, Mapping):
                continue
            rel_path = normalize_material_path(item.get("path"))
            try:
                data = self._bucket.get_object(
                    self.material_key(skill_name, dataset_id, rel_path)
                ).read()
            except Exception as exc:
                if not skill_name or not is_not_found_error(exc):
                    raise
                data = self._bucket.get_object(
                    self.legacy_material_key(skill_name, dataset_id, rel_path)
                ).read()
            files.append((rel_path, data))
        return files

    def available_material_paths(
        self,
        dataset: Mapping[str, Any],
    ) -> list[str]:
        """List only material entries whose object bytes actually exist."""
        skill_ids = [
            str(item or "")
            for item in dataset.get("skill_ids") or []
            if str(item or "")
        ]
        skill_name = str(dataset.get("skill_name") or next(iter(skill_ids), ""))
        dataset_id = normalize_dataset_id(dataset.get("dataset_id"))
        paths: list[str] = []
        for item in dataset_material_entries(dataset):
            if not isinstance(item, Mapping) or not item.get("path"):
                continue
            rel_path = normalize_material_path(item.get("path"))
            try:
                self._bucket.get_object(
                    self.material_key(skill_name, dataset_id, rel_path)
                )
            except Exception as exc:
                if not is_not_found_error(exc):
                    raise
                if not skill_name:
                    continue
                try:
                    self._bucket.get_object(
                        self.legacy_material_key(
                            skill_name,
                            dataset_id,
                            rel_path,
                        )
                    )
                except Exception as legacy_exc:
                    if is_not_found_error(legacy_exc):
                        continue
                    raise
            paths.append(rel_path)
        return paths

    def delete_dataset(self, *, skill_name: str, dataset_id: str) -> bool:
        prefixes = [self.dataset_prefix("", dataset_id)]
        if skill_name:
            prefixes.append(self.legacy_dataset_prefix(skill_name, dataset_id))
        objects = [
            item
            for prefix in prefixes
            for item in self._bucket.iter_objects(prefix=prefix)
        ]
        for obj in objects:
            self._bucket.delete_object(obj.key)
        return bool(objects)
