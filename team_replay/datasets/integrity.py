"""Canonical object-store repository for Skill-owned datasets."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

from .schema import (
    DATASET_SCHEMA,
    normalize_skill_refs,
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
        dataset.get("schema_version") == DATASET_SCHEMA
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
        dataset.get("schema_version") == DATASET_SCHEMA
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


