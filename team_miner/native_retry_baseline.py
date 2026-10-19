"""Restore the last accepted raw target before an explicitly authorized, unaccepted retry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException

PROTOCOL = "te-native-retry-baseline.v1"
_METHOD_FILES = {"SKILL.md", "METHOD.json", "EVALUATION.md", "benchmark.json"}
_HOST_REPORTS = {"semantic_reports/capability-plan.json", "semantic_reports/evidence-map.md",
                 "semantic_reports/gaps.md", "semantic_reports/review.json"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bytes(root: Path, path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
        raise ValueError("验收基线要求任务目录内的普通冻结文件")
    return path.read_bytes()


def _hashes(root: Path, directory: Path) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("缺少紧邻前轮的完整产物归档")
    return {path.relative_to(directory).as_posix(): _sha(_bytes(root, path))
            for path in sorted(directory.rglob("*")) if path.is_file()}


def _verify_component_projection(
    root: Path, archive: Path, raw: Path, expected: dict, allowed: set[str],
    frozen_sources: dict[str, bytes] | None = None,
) -> None:
    """Replay structural assembly in isolation; no derived file is uploaded to the target."""
    from .native_components import assemble_components

    with tempfile.TemporaryDirectory(prefix="te-native-retry-components-") as folder:
        projection = Path(folder) / "output"
        shutil.copytree(raw, projection)
        audits = assemble_components(projection, allowed, required=True, frozen_sources=frozen_sources)
        if not audits:
            raise ValueError("前轮组件缺少可验证的装配输入")
        for audit in audits:
            name = audit["name"]
            relative = f"semantic_reports/components/{name}/assembly-audit.json"
            original = _bytes(root, archive / "output" / relative)
            if json.loads(original) != audit:
                raise ValueError("前轮组件装配审计与原始输入不等价")
            copied = {item["path"] for item in audit.get("source_copies", [])}
            for item in audit["inputs"]:
                content = ((projection / item["path"]).read_bytes() if item["path"] in copied
                           else _bytes(root, raw / item["path"]))
                if _sha(content) != item["sha256"]:
                    raise ValueError("前轮组件装配输入 SHA 不一致")
            output = audit["output"]["path"]
            if output not in expected:
                output = output.replace("compiled_skill/", "semantic_reports/lineage/", 1)
            if expected.get(output) != audit["output"]["sha256"]:
                raise ValueError("前轮组件装配结果与验收产物不等价")


def baseline_plan(root: Path, job: dict) -> dict:
    """Prove raw bytes against both the archived accepted output and unchanged human review."""
    number = int(job["current_round"])
    previous = number - 1
    review = job.get("last_checkpoint_submission") or {}
    if previous < 1 or review.get("round") != previous or review.get("action") != "continue":
        raise ValueError("人工重试缺少紧邻前轮的真实复核记录")
    archive = root / "rounds" / f"round-{previous}"
    metadata_bytes = _bytes(root, archive / "meta.json")
    meta = json.loads(metadata_bytes)
    expected = meta.get("output_sha256")
    if (meta.get("round") != previous or meta.get("output_uri") != job.get("stable_output_uri")
            or not isinstance(expected, dict) or not expected
            or _hashes(root, archive / "output") != expected
            or _hashes(root, archive / "reviewed_output") != expected):
        raise ValueError("紧邻前轮验收产物或人工复核版本已变化，不能猜测远端基线")
    components = {Path(relative).parts[2] for relative in expected
                  if len(Path(relative).parts) == 4
                  and Path(relative).parts[:2] == ("semantic_reports", "components")
                  and Path(relative).name == "manifest.json"}
    copied_sources = set()
    for name in components:
        manifest = json.loads(_bytes(root, archive / "output" / "semantic_reports/components" / name / "manifest.json"))
        copied_sources.update(row["path"] for row in manifest.get("references", []) if "copy_source" in row)
    desired, optional = {}, {}
    for relative, digest in expected.items():
        path = Path(relative)
        # METHOD supersession relocates an accepted package without changing its raw identity.
        if path.parts[:2] == ("semantic_reports", "lineage"):
            path = Path("compiled_skill", *path.parts[2:])
        if len(path.parts) == 3 and path.parts[0] == "compiled_skill" and path.name in _METHOD_FILES:
            target = optional if path.parts[1] in components and path.name == "benchmark.json" else desired
            target[path.as_posix()] = digest
        elif (len(path.parts) >= 4 and path.parts[0] == "compiled_skill" and path.parts[1] in components
              and path.parts[2] == "references" and path.suffix in {".md", ".json"}):
            target = optional if path.as_posix() in copied_sources else desired
            target[path.as_posix()] = digest
        elif len(path.parts) == 3 and path.parts[:2] == ("semantic_reports", "notes") and path.suffix == ".json":
            desired["analysis/notes/" + path.name] = digest
        elif len(path.parts) >= 4 and path.parts[:2] == ("semantic_reports", "components"):
            if len(path.parts) == 4 and path.name == "assembly-audit.json" and path.parts[2] in components:
                continue  # Recomputed below; this is a local acceptance receipt, not a Native output.
            if (path.parts[2] not in components or not (
                    len(path.parts) == 4 and path.name == "manifest.json"
                    or len(path.parts) == 5 and path.parts[3] == "cases" and path.suffix == ".json")):
                raise ValueError("前轮包含未声明的组件原始路径")
            desired[Path("analysis", *path.parts[1:]).as_posix()] = digest
        elif relative in _HOST_REPORTS or (
                len(path.parts) == 3 and path.parts[0] == "compiled_skill" and path.name == "BENCHMARK.md"):
            continue
        else:
            raise ValueError("前轮含无法明确映射的原始产物，不能恢复远端基线")
    names = {Path(path).parts[1] for path in desired if path.startswith("compiled_skill/")}
    mapped = set(desired) | set(optional)
    if not names or any({f"compiled_skill/{name}/{filename}" for filename in _METHOD_FILES} - mapped
                        for name in names):
        raise ValueError("前轮基线缺少完整候选文件包")
    for candidate in sorted((root / "round_outputs" / f"round-{previous}").glob("raw-*")):
        raw_hashes = _hashes(root, candidate)
        # Some older producers also wrote an equivalent dataset. Its existing bytes
        # may be restored, but a dataset created only by TE must stay local.
        raw_optional = {path: digest for path, digest in optional.items() if path in raw_hashes}
        if raw_hashes == desired | raw_optional:
            if components:
                from .native_source_resources import frozen_source_resources

                _verify_component_projection(root, archive, candidate, expected,
                                             set(job.get("native_allowed_source_uris") or []),
                                             frozen_source_resources(root, job, previous) if copied_sources else None)
            return {"schema_version": PROTOCOL, "job_id": job["job_id"], "round": number,
                    "baseline_round": previous, "to": job["stable_output_uri"], "meta_sha256": _sha(metadata_bytes),
                    "raw_dir": candidate.relative_to(root).as_posix(), "files": raw_hashes}
    raise ValueError("找不到与前轮全部验收 SHA 对应的完整原始文件包")


async def _target(owner, job: dict, desired: dict) -> dict[str, bytes]:
    from .ov_compile_mining import validate_descendant
    from .routes import _content_text

    output = {}
    total = 0
    async for item in owner._iter_knowledge_entries(job["account"], job["stable_output_uri"]):
        if item.get("is_dir"):
            continue
        uri = validate_descendant(job["stable_output_uri"], item.get("uri"), allow_root=False)
        relative = uri[len(job["stable_output_uri"]) + 1:]
        if any(part.startswith(".") for part in relative.split("/")):
            continue
        if relative not in desired or relative in output:
            raise ValueError("远端存在额外或重复文件，拒绝覆盖未确认的产物")
        response = await owner._knowledge_request(job["account"], "GET", "/api/v1/content/read",
            params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"})
        data = _content_text(response).encode("utf-8")
        total += len(data)
        if len(data) > 20 * 1024 * 1024 or total > 64 * 1024 * 1024:
            raise ValueError("远端重试基线超过有界归档容量")
        output[relative] = data
    if set(output) != set(desired):
        raise ValueError("远端文件集合与已验收原始基线不一致")
    return output


async def restore_baseline(owner, job: dict) -> dict | None:
    """Return an existing acceptance, or archive/restore only after an authoritative 404."""
    retry = job.get("native_explicit_retry") or {}
    if retry.get("round") != job.get("current_round"):
        return None
    key = job["submission_idempotency_key"]
    try:
        accepted = await owner._knowledge_request(job["account"], "GET", f"/api/v1/compile/submissions/{key}",
                                                timeout=10.0)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        if not isinstance(accepted, dict) or not accepted.get("task_id"):
            raise ValueError("重试受理对账缺少准确 Native 身份，拒绝回滚目标")
        return accepted
    from .native_recovery import pending_guidance
    from .ov_compile_mining import utc_now
    from .ov_round_orchestration import _write_json

    repository = owner._ov_skill_mining_repository()
    root = repository.jobs_root / job["job_id"]
    await asyncio.to_thread(pending_guidance, root, job, "")
    plan = await asyncio.to_thread(baseline_plan, root, job)
    desired = plan["files"]
    folder = root / "round_inputs" / f"round-{job['current_round']}" / "native-explicit-retry" / (
        str(uuid.UUID(retry["authorization"]["request_id"])) + "-baseline")
    manifest_path, complete_path = folder / "manifest.json", folder / "complete.json"
    current = await _target(owner, job, desired)
    if manifest_path.exists():
        manifest = json.loads(await asyncio.to_thread(_bytes, root, manifest_path))
        if (manifest.get("plan") != plan or manifest.get("submission_key") != key
                or set(manifest.get("partial_sha256") or {}) != set(desired)):
            raise ValueError("重试基线恢复记录不属于本次冻结任务")
        for relative, digest in manifest["partial_sha256"].items():
            data = await asyncio.to_thread(_bytes, root, folder / "partial" / relative)
            if _sha(data) != digest:
                raise ValueError("重试前远端部分结果的原始归档已改变")
    else:
        # Complete the entire byte archive before the first remote mutation.
        for relative, data in current.items():
            path = folder / "partial" / relative
            if path.exists():
                if await asyncio.to_thread(_bytes, root, path) != data:
                    raise ValueError("未完成归档出现第三种远端内容，拒绝覆盖")
            else:
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, data)
        manifest = {"schema_version": PROTOCOL, "plan": plan, "submission_key": key,
                    "partial_sha256": {path: _sha(data) for path, data in current.items()}, "archived_at": utc_now()}
        await asyncio.to_thread(_write_json, manifest_path, manifest)
    manifest_sha = _sha(await asyncio.to_thread(_bytes, root, manifest_path))
    if complete_path.exists():
        complete = json.loads(await asyncio.to_thread(_bytes, root, complete_path))
        if complete.get("manifest_sha256") != manifest_sha or complete.get("submission_key") != key:
            raise ValueError("基线恢复完成记录已改变")
        if {path: _sha(data) for path, data in current.items()} != desired:
            raise ValueError("已恢复基线出现新内容，拒绝再次回滚可能已受理的结果")
        return None
    for relative, data in current.items():
        if _sha(data) not in {manifest["partial_sha256"][relative], desired[relative]}:
            raise ValueError("基线恢复遇到第三种远端内容，拒绝覆盖")
    for relative in sorted(desired):
        if _sha(current[relative]) == desired[relative]:
            continue
        data = await asyncio.to_thread(_bytes, root, root / plan["raw_dir"] / relative)
        if _sha(data) != desired[relative]:
            raise ValueError("已验收原始基线在恢复期间发生变化")
        await owner._knowledge_request(job["account"], "POST", "/api/v1/content/write", timeout=180.0,
            json={"uri": plan["to"] + "/" + relative, "content": data.decode("utf-8"),
                  "mode": "replace", "wait": True})
    restored = await _target(owner, job, desired)
    if {path: _sha(data) for path, data in restored.items()} != desired:
        raise ValueError("恢复后回读的全部文件 SHA 未与已验收基线一致")
    await asyncio.to_thread(_write_json, complete_path,
        {"schema_version": PROTOCOL, "submission_key": key, "manifest_sha256": manifest_sha,
         "completed_at": utc_now(), "target_sha256": desired})
    return None
