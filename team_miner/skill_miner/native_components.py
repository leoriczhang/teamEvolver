"""Validate and assemble model-authored components without creating business content.

The manifest is a transport contract. Its coverage rows are declarations, not
proof that a rule was understood or that the resulting Skill is correct.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from team_replay.datasets.schema import validate_document

PROTOCOL = "te-native-components.v1"
SOURCE_RESOURCE_PROTOCOL = "te-native-components.v2"
SUPPORTED_PROTOCOLS = {PROTOCOL, SOURCE_RESOURCE_PROTOCOL}
MAX_PACKAGES = 256
MAX_REFERENCES = 512
MAX_COVERAGE = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MANIFEST_FIELDS = {"schema_version", "name", "dataset", "references", "cases", "coverage"}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"组件 JSON 字段重复：{key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> Any:
    def invalid_constant(value):
        raise ValueError(f"组件 JSON 不允许非有限数值：{value}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"组件不是有效 UTF-8 JSON：{label}: {exc}") from exc

def _load_json(raw: bytes, label: str) -> dict:
    value = _decode_json(raw, label)
    if not isinstance(value, dict):
        raise ValueError(f"组件必须是 JSON 对象：{label}")
    return value


def _path(root: Path, value: Any) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or ":" in value or len(value) > 1024):
        raise ValueError("组件路径必须是安全的目标内相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"组件路径不规范或越界：{value}")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f"组件路径不能包含符号链接：{value}")
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"组件路径越界：{value}")
    return target


def _files(root: Path, relative: str) -> set[str]:
    folder = _path(root, relative)
    if not folder.exists():
        return set()
    if not folder.is_dir():
        raise ValueError(f"组件目录无效：{relative}")
    result = set()
    for current, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            candidate = Path(current) / name
            rel = candidate.relative_to(root).as_posix()
            _path(root, rel)
            if name in files:
                if not candidate.is_file():
                    raise ValueError(f"组件必须是普通文件：{rel}")
                result.add(rel)
    return result


def _strings(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{label} 必须是无重复的非空文字数组")
    return value


def assemble_components(
    destination: Path, allowed_sources: set[str], *, required: bool = False,
    frozen_sources: dict[str, bytes] | None = None,
) -> list[dict] | None:
    """Validate every package before writing any dataset; retain all source bytes.

    Legacy output is left untouched when no component manifest exists and the
    frozen compiler did not require this protocol. Callers still run the usual
    artifact and business acceptance gates after assembly.
    """
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("组件目标不能是符号链接")
    root = destination.resolve()
    component_files = _files(root, "analysis/components")
    manifest_paths = sorted(path for path in component_files if PurePosixPath(path).name == "manifest.json")
    methods = sorted((root / "compiled_skill").glob("*/METHOD.json"))
    if not manifest_paths:
        if component_files or (required and methods):
            raise ValueError("组件输出缺少 manifest.json")
        return None
    if len(manifest_paths) > MAX_PACKAGES:
        raise ValueError("组件包数量超过传输上限")

    total_bytes = 0
    cache: dict[str, bytes] = {}

    def read(relative: str) -> bytes:
        nonlocal total_bytes
        if relative not in cache:
            path = _path(root, relative)
            if not path.is_file():
                raise ValueError(f"组件缺少文件：{relative}")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
                raise ValueError("组件内容超过传输大小上限")
            raw = path.read_bytes()
            if len(raw) != size:
                raise ValueError(f"组件文件在装配期间变化：{relative}")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"组件必须为 UTF-8 文本：{relative}") from exc
            total_bytes += size
            cache[relative] = raw
        return cache[relative]

    prepared = []
    copies: dict[str, bytes] = {}
    declared_components = set()
    names = set()
    for manifest_path in manifest_paths:
        manifest = _load_json(read(manifest_path), manifest_path)
        name = manifest.get("name")
        protocol = manifest.get("schema_version")
        if (set(manifest) != _MANIFEST_FIELDS or protocol not in SUPPORTED_PROTOCOLS
                or not isinstance(name, str) or not _SAFE_ID.fullmatch(name) or name in names):
            raise ValueError("组件清单字段、协议或方法标识无效")
        names.add(name)
        prefix = f"analysis/components/{name}"
        candidate = f"compiled_skill/{name}"
        if manifest_path != f"{prefix}/manifest.json":
            raise ValueError("组件清单与方法目录不一致")
        inputs = {manifest_path}
        declared_components.add(manifest_path)
        core = [f"{candidate}/{filename}" for filename in ("METHOD.json", "SKILL.md", "EVALUATION.md")]
        for path in core:
            if not read(path).strip():
                raise ValueError(f"组件方法正文不能为空：{path}")
            inputs.add(path)
        method = _load_json(read(core[0]), core[0])
        if method.get("schema_version") != "skill-miner.method.v1" or method.get("name") != name:
            raise ValueError("组件 METHOD 身份或协议无效")
        method_refs = set(_strings(method.get("source_refs"), "METHOD.source_refs"))
        if not method_refs <= allowed_sources:
            raise ValueError("组件 METHOD 含未知来源")

        def source_refs(value):
            refs = set(_strings(value, "组件 source_refs"))
            if not refs <= method_refs or not refs <= allowed_sources:
                raise ValueError("组件含未知来源或来源超出 METHOD")
            return refs

        references = manifest["references"]
        if not isinstance(references, list) or len(references) > MAX_REFERENCES:
            raise ValueError("组件 references 数组无效或超过上限")
        reference_paths = set()
        reference_sources = {}
        source_copies = []
        for row in references:
            fields = set(row) if isinstance(row, dict) else set()
            copy_source = row.get("copy_source") if isinstance(row, dict) else None
            is_copy = fields == {"path", "source_refs", "copy_source"}
            if not (fields == {"path", "source_refs"} or is_copy and protocol == SOURCE_RESOURCE_PROTOCOL):
                raise ValueError("组件 reference 声明字段无效")
            path = row["path"]
            _path(root, path)
            if (not path.startswith(f"{candidate}/references/") or PurePosixPath(path).suffix not in {".md", ".json"}
                    or path in reference_paths):
                raise ValueError("组件 reference 路径越界、类型无效或重复")
            reference_sources[path] = source_refs(row["source_refs"])
            if is_copy:
                if (not isinstance(copy_source, str) or reference_sources[path] != {copy_source}
                        or frozen_sources is None or copy_source not in frozen_sources):
                    raise ValueError("原文资源必须唯一绑定允许的完整冻结源 URI")
                raw = frozen_sources[copy_source]
                if not isinstance(raw, bytes):
                    raise ValueError("原文资源必须使用已核验的冻结字节")
                if _path(root, path).exists():
                    if read(path) != raw:
                        raise ValueError("现有原文资源与冻结来源不一致，拒绝覆盖")
                else:
                    if len(raw) > MAX_FILE_BYTES or total_bytes + len(raw) > MAX_TOTAL_BYTES:
                        raise ValueError("组件内容超过传输大小上限")
                    raw.decode("utf-8")
                    total_bytes += len(raw)
                    cache[path] = raw
                    copies[path] = raw
                source_copies.append({"path": path, "source_ref": copy_source,
                                      "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            content = read(path)
            if not content.strip():
                raise ValueError("组件 reference 内容不能为空")
            if path.endswith(".json"):
                # References may be JSON arrays as well as objects; preserve bytes.
                _decode_json(content, path)
            reference_paths.add(path)
            inputs.add(path)
        if _files(root, f"{candidate}/references") | {row["path"] for row in source_copies} != reference_paths:
            raise ValueError("组件包含未声明的 reference 文件")

        descriptors = manifest["cases"]
        if not isinstance(descriptors, list) or len(descriptors) != 12:
            raise ValueError("组件清单必须声明恰好 12 个 Case")
        cases, case_ids, case_paths = [], set(), set()
        case_sources = {}
        for row in descriptors:
            if not isinstance(row, dict) or set(row) != {"path", "case_id", "source_refs"}:
                raise ValueError("组件 Case 声明字段无效")
            path, case_id = row["path"], row["case_id"]
            _path(root, path)
            parsed = PurePosixPath(path)
            if (parsed.parent.as_posix() != f"{prefix}/cases" or parsed.suffix != ".json"
                    or not _SAFE_ID.fullmatch(parsed.stem) or path in case_paths):
                raise ValueError("组件 Case 路径越界、类型无效或重复")
            if not isinstance(case_id, str) or not case_id.strip() or case_id in case_ids:
                raise ValueError("组件 Case ID 为空或重复")
            refs = source_refs(row["source_refs"])
            case = _load_json(read(path), path)
            if case.get("case_id") != case_id or case.get("skill_ids") != [name]:
                raise ValueError("组件 Case 身份与声明不一致")
            provenance = case.get("provenance")
            if not isinstance(provenance, dict) or source_refs(provenance.get("session_ids")) != refs:
                raise ValueError("组件 Case 来源与声明不一致")
            cases.append(case)
            case_ids.add(case_id)
            case_sources[case_id] = refs
            case_paths.add(path)
            inputs.add(path)
            declared_components.add(path)

        coverage = manifest["coverage"]
        if not isinstance(coverage, list) or not coverage or len(coverage) > MAX_COVERAGE:
            raise ValueError("组件 coverage 数组无效或超过上限")
        covered = set()
        for row in coverage:
            if (not isinstance(row, dict)
                    or set(row) != {"source_ref", "reference_paths", "case_ids", "disposition", "reason"}):
                raise ValueError("组件 coverage 声明字段无效")
            ref = row["source_ref"]
            if not isinstance(ref, str) or ref not in method_refs or ref in covered:
                raise ValueError("组件 coverage 来源未知或重复")
            linked_references = set(_strings(row["reference_paths"], "coverage.reference_paths", allow_empty=True))
            if not linked_references <= reference_paths:
                raise ValueError("组件 coverage 引用了未声明的 reference")
            linked_cases = set(_strings(row["case_ids"], "coverage.case_ids", allow_empty=True))
            if not linked_cases <= case_ids:
                raise ValueError("组件 coverage 引用了未声明的 Case")
            if (any(ref not in reference_sources[path] for path in linked_references)
                    or any(ref not in case_sources[case_id] for case_id in linked_cases)):
                raise ValueError("组件 coverage 来源与所引用组件的来源声明不一致")
            if any(not isinstance(row[key], str) or not row[key].strip() for key in ("disposition", "reason")):
                raise ValueError("组件 coverage 必须保留处置及理由")
            covered.add(ref)
        if covered != method_refs:
            raise ValueError("组件 coverage 未完整枚举 METHOD 来源")

        header = manifest["dataset"]
        if not isinstance(header, dict) or "cases" in header:
            raise ValueError("组件 dataset 必须为不含 cases 的完整头部")
        skills = header.get("skills")
        if (not isinstance(skills, list) or len(skills) != 1 or not isinstance(skills[0], dict)
                or skills[0].get("skill_id") != name):
            raise ValueError("组件 dataset 必须仅属于当前 Skill")
        dataset_source = header.get("source")
        if not isinstance(dataset_source, dict):
            raise ValueError("组件 dataset.source 无效")
        if "session_ids" in dataset_source:
            refs = set(_strings(dataset_source["session_ids"], "dataset.source.session_ids", allow_empty=True))
            if not refs <= method_refs or not refs <= allowed_sources:
                raise ValueError("组件 dataset 含未知来源或来源超出 METHOD")
        dataset = {**header, "cases": cases}
        errors = validate_document(dataset)
        if errors:
            raise ValueError("组件 Test Dataset 契约无效：" + "; ".join(errors))
        benchmark = f"{candidate}/benchmark.json"
        output = _json_bytes(dataset)
        if _path(root, benchmark).exists():
            existing = read(benchmark)
            if _json_bytes(_load_json(existing, benchmark)) != output:
                raise ValueError("现有 benchmark.json 与组件装配结果冲突")
            output = existing
        audit_path = f"{prefix}/assembly-audit.json"
        declared_components.add(audit_path)
        audit = {"schema_version": protocol, "name": name, "coverage_is_semantic_proof": False,
                 "inputs": [{"path": path, "sha256": hashlib.sha256(read(path)).hexdigest(),
                             "bytes": len(read(path))} for path in sorted(inputs)],
                 "output": {"path": benchmark, "sha256": hashlib.sha256(output).hexdigest(),
                            "bytes": len(output)}, "case_ids": [case["case_id"] for case in cases]}
        if protocol == SOURCE_RESOURCE_PROTOCOL:
            audit["source_copies"] = source_copies
        if _path(root, audit_path).exists() and _load_json(read(audit_path), audit_path) != audit:
            raise ValueError("已有组件装配审计与当前输入不一致")
        prepared.append((benchmark, output, audit_path, audit))

    if component_files - declared_components:
        raise ValueError("组件目录存在未声明的组件文件")
    if required and {path.parent.name for path in methods} != names:
        raise ValueError("每个 METHOD 必须有匹配的组件清单")
    # The destination is a temporary materialization directory. Do not write
    # anything until every package, including sibling packages, has passed.
    for relative, raw in copies.items():
        path = _path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for benchmark, output, audit_path, audit in prepared:
        _path(root, benchmark).write_bytes(output)
        _path(root, audit_path).write_bytes(_json_bytes(audit))
    return [audit for _, _, _, audit in prepared]
