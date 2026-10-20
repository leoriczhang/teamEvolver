"""Bounded, explicit request-contract upgrades; accepted compiler and Evidence remain immutable."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .native_compiler import PROTOCOL, frozen_assets

RECOVERY_PROTOCOL = "te-native-contract-recovery.v1"
MAX_ROUND_UPGRADES = 2
EXPLICIT_RETRY_PROTOCOL = "te-native-explicit-retry.v1"


def effective_round_budget(job: dict) -> tuple[str, int]:
    """A separately authorized attempt has its own budget; original round timing stays intact."""
    retry = job.get("native_explicit_retry") or {}
    if retry.get("round") == job.get("current_round"):
        budget = retry["attempt_budget"]
        return str(budget["started_at"]), int(budget["seconds"])
    return (str(job.get("round_started_at") or job.get("started_at") or ""),
            int(job.get("round_timeout_seconds") or job.get("runtime_timeout_seconds") or 3600))


def ensure_round_budget(job: dict) -> str:
    """Validate the existing round budget; retry and recovery never create a new start time."""
    start, timeout = effective_round_budget(job)
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("本轮缺少有效预算起点，不能通过恢复续时") from exc
    if (datetime.now(timezone.utc) - parsed).total_seconds() >= timeout:
        raise ValueError("本轮已有恢复预算已到期，不能通过契约升级续时或重新提交")
    return start


def _sha(root: Path, path: Path) -> str:
    if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
        raise ValueError("恢复核验要求任务目录内的普通文件")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_preserved(root: Path, hashes: dict) -> None:
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("恢复记录缺少冻结文件摘要")
    for name, expected in hashes.items():
        if _sha(root, root / name) != expected:
            raise ValueError("恢复前已冻结的请求、来源或产物发生变化")


def _error_code(error) -> str:
    if isinstance(error, dict):
        return str(error.get("code") or "")
    match = re.search(r"\b([A-Z][A-Z0-9_]+):", str(error or ""))
    return match[1] if match else ""


def _event(failure: dict) -> dict:
    diagnosis = failure["diagnostics"]
    return {"kind": failure["kind"], "missing_uris": failure["missing_uris"],
            "code": _error_code(diagnosis.get("error")),
            "errors": diagnosis["pipeline"].get("errors") or []}


def _failure(job: dict) -> dict:
    from .native_failures import classify_native_failure

    saved = job.get("native_failure") or {}
    if saved.get("task_id") == job.get("ov_task_id"):
        task = {"error": saved["diagnostics"]["error"], "meta": {"pipeline": saved["diagnostics"]["pipeline"]}}
    else:
        task = {"error": job.get("error"), "meta": {"pipeline": job.get("ov_pipeline") or {}}}
    return classify_native_failure(task, compiler_uri=job["compiler_uri"])


def _verify_terminal(job: dict, failure: dict, task: dict | None) -> None:
    from .native_failures import classify_native_failure

    if (not task or task.get("task_id") != job.get("ov_task_id")
            or task.get("status") not in {"failed", "completed", "succeeded"}):
        raise ValueError("恢复前必须读取准确远端任务并确认已结束的协议失败")
    if _event(classify_native_failure(task, compiler_uri=job["compiler_uri"])) != _event(failure):
        raise ValueError("准确远端任务与本地归档不是同一失败事件")


def _frozen_request(root: Path, job: dict) -> tuple[dict, dict]:
    from .ov_round_orchestration import _cached_inputs, _compiler_package, _digest

    _sha(root, root / "compiler-skill.md")
    compiler = (root / "compiler-skill.md").read_text(encoding="utf-8")
    name, published, _ = _compiler_package(compiler)
    assets = frozen_assets(root, compiler)
    request = job.get("compile_request") or {}
    if (assets is None or _digest(compiler) != job.get("compiler_source_sha256")
            or _digest(published) != job.get("compiler_sha256")
            or job.get("compiler_uri") != f"viking://agent/skills/{name}"
            or request.get("skill") != job.get("compiler_uri")
            or request.get("to") != job.get("stable_output_uri")
            or request.get("from") != [job.get("source_snapshot")]):
        raise ValueError("恢复前冻结的编译 Skill、来源或目标不一致")
    number = int(job["current_round"])
    for previous in range(1, number + 1):
        if _cached_inputs(root / "round_inputs" / f"round-{previous}") is None:
            raise ValueError("恢复缺少冻结输入")
    path = root / "round_inputs" / f"round-{number}" / "native-requests" / (
        _digest(job["submission_idempotency_key"]) + ".json")
    _sha(root, path)
    if json.loads(path.read_text(encoding="utf-8")) != request:
        raise ValueError("恢复前原始 Compile 请求与受理记录不一致")
    accepted = [row for row in job.get("native_compile_history", []) if row["task_id"] == job.get("ov_task_id")]
    if (len(accepted) != 1 or accepted[0]["submission_key"] != job["submission_idempotency_key"]
            or accepted[0]["round"] != number):
        raise ValueError("恢复缺少精确的已结束 Native 受理记录")
    for previous in range(1, number):
        archive = root / "rounds" / f"round-{previous}"
        meta = json.loads((archive / "meta.json").read_text(encoding="utf-8"))
        for filename, digest in meta["output_sha256"].items():
            if _sha(root, archive / "output" / filename) != digest:
                raise ValueError("已验收轮次的产物归档发生变化")
    return assets, request


def _previous_guidance(instruction: str, assets: dict) -> str:
    """Read machine-owned guidance from the accepted request, including the legacy suffix."""
    from .native_compiler import GUIDANCE_END, GUIDANCE_START, resource_policy

    if GUIDANCE_START in instruction:
        before, marker, rest = instruction.rpartition(GUIDANCE_START)
        if not marker or GUIDANCE_END not in rest or GUIDANCE_START in before:
            raise ValueError("原请求的契约指导边界无效")
        body, _, after = rest.partition(GUIDANCE_END)
        if after.strip():
            raise ValueError("原请求的契约指导不是完整后缀")
        return GUIDANCE_START + body + GUIDANCE_END
    legacy = resource_policy(assets)
    return legacy if legacy and instruction.endswith(legacy) else ""


def _guidance_content(guidance: str) -> str:
    from .native_compiler import GUIDANCE_END, GUIDANCE_START

    if guidance.startswith(GUIDANCE_START) and guidance.endswith(GUIDANCE_END):
        guidance = guidance[len(GUIDANCE_START):-len(GUIDANCE_END)]
    return guidance.strip()


def pending_guidance(root: Path, job: dict, current: str) -> str:
    """An approved upgrade keeps its exact guidance if software changes before its first POST."""
    from .ov_round_orchestration import _digest

    retry = job.get("native_explicit_retry") or {}
    if retry.get("round") == job.get("current_round"):
        path = root / "round_inputs" / f"round-{job['current_round']}" / "native-explicit-retry" / (
            str(uuid.UUID(retry["authorization"]["request_id"])) + ".json")
        _sha(root, path)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (receipt.get("protocol") != EXPLICIT_RETRY_PROTOCOL or receipt.get("job_id") != job["job_id"]
                or receipt.get("retry") != retry
                or _digest(receipt["guidance"]) != retry["guidance_sha256"]
                or _digest(receipt["format_guidance"]) != retry["format_guidance_sha256"]):
            raise ValueError("本次人工重试的冻结指导或授权已改变")
        from .native_retry_baseline import baseline_plan
        if baseline_plan(root, job) != receipt.get("baseline_plan"):
            raise ValueError("本次人工重试的已验收原始基线发生变化")
        request = receipt["request"]
        if (request.get("skill") != job.get("compiler_uri")
                or request.get("from") != [job.get("source_snapshot")]
                or request.get("to") != job.get("stable_output_uri")):
            raise ValueError("本次人工重试的原来源、目标或编译 Skill 已改变")
        _verify_preserved(root, receipt["preserved_sha256"])
        return receipt["guidance"]
    entries = [row for row in job.get("native_contract_recoveries", [])
               if row.get("round") == job["current_round"]
               and row.get("submission_key") == job.get("submission_idempotency_key")]
    if not entries:
        return current
    if len(entries) != 1:
        raise ValueError("本轮待提交契约升级标识重复")
    entry = entries[0]
    digest = entry.get("guidance_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("待提交请求指导摘要无效")
    path = root / "round_inputs" / f"round-{job['current_round']}" / "native-recoveries" / f"{digest}.json"
    _sha(root, path)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (receipt.get("protocol") != RECOVERY_PROTOCOL or receipt.get("job_id") != job["job_id"]
            or any(receipt.get(key) != entry.get(key) for key in
                   ("round", "task_id", "submission_key", "guidance_sha256", "upgrade_index"))):
        raise ValueError("待提交请求指导不属于当前恢复事件")
    guidance = receipt.get("guidance")
    if not isinstance(guidance, str) or not guidance or _digest(guidance) != digest:
        raise ValueError("待提交请求指导已改变")
    expected_key = (f"te-sm-{job['job_id']}-r{job['current_round']}-upgrade-"
                    + _digest(entry["task_id"] + digest)[:24])
    request = receipt.get("request") or {}
    if (entry["submission_key"] != expected_key or request.get("skill") != job.get("compiler_uri")
            or request.get("from") != [job.get("source_snapshot")]
            or request.get("to") != job.get("stable_output_uri")):
        raise ValueError("待提交请求的来源、目标、编译 Skill 或标识发生变化")
    _verify_preserved(root, receipt["preserved_sha256"])
    return guidance


def prepare_recovery(root: Path, job: dict, *, apply: bool = False, remote_task: dict | None = None) -> dict:
    from .native_compiler import request_guidance
    from .ov_round_orchestration import _digest, _write_json

    if (job.get("execution_protocol") != PROTOCOL or job.get("status") != "failed"
            or job.get("ov_status") not in {"failed", "completed", "succeeded"} or job.get("ov_cancel_reason")):
        raise ValueError("任务不在可恢复的原生请求契约失败边界")
    number = int(job["current_round"])
    upgrades = [row for row in job.get("native_contract_recoveries", []) if row["round"] == number]
    legacy = job.get("native_resource_recovery") or {}
    used = len(upgrades) + int(legacy.get("round") == number)
    if used >= MAX_ROUND_UPGRADES:
        raise ValueError("本轮请求契约升级已达到两次上限，不能继续授予重试")
    budget_start = ensure_round_budget(job)
    assets, request = _frozen_request(root, job)
    guidance = request_guidance(assets)
    guidance_sha = _digest(guidance)
    instruction = str(request.get("instruction") or "")
    old_guidance = _previous_guidance(instruction, assets)
    if (guidance.strip() in instruction or _guidance_content(old_guidance) == _guidance_content(guidance)
            or any(row.get("guidance_sha256") == guidance_sha for row in upgrades)):
        raise ValueError("同一契约指导版本不能重复恢复，必须先修复并升级请求契约")
    failure = _failure(job)
    if failure["kind"] == "compiler_dependency":
        prefix = job["compiler_uri"].rstrip("/") + "/"
        if (not failure["dependency_uris"] or set(failure["missing_uris"]) != set(failure["dependency_uris"])
                or any(not uri.startswith(prefix) or uri[len(prefix):] in assets
                       for uri in failure["dependency_uris"])):
            raise ValueError("真实依赖或来源缺失不能用请求契约升级恢复")
    else:
        raise ValueError("仅明确的未声明编译附件调用失败可使用附件边界指导恢复，输出协议失败须独立修复")
    if apply:
        _verify_terminal(job, failure, remote_task)
    key = f"te-sm-{job['job_id']}-r{number}-upgrade-{_digest(job['ov_task_id'] + guidance_sha)[:24]}"
    receipt_path = root / "round_inputs" / f"round-{number}" / "native-recoveries" / f"{guidance_sha}.json"
    expected = {"protocol": RECOVERY_PROTOCOL, "job_id": job["job_id"], "round": number,
                "task_id": job["ov_task_id"], "request": request, "error": job["error"],
                "failed_at": job["finished_at"], "guidance": guidance, "guidance_sha256": guidance_sha,
                "old_guidance_sha256": _digest(old_guidance), "submission_key": key, "upgrade_index": used + 1}
    if receipt_path.exists():
        _sha(root, receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if any(receipt.get(field) != value for field, value in expected.items()):
            raise ValueError("本次失败或契约版本与已记录的恢复事件不一致")
        _verify_preserved(root, receipt["preserved_sha256"])
        return receipt
    preserved = {p.relative_to(root).as_posix(): _sha(root, p) for p in sorted(root.rglob("*"))
                 if p.is_file() and p not in {root / "job.json", root / "run.log"}}
    receipt = {**expected, "diagnosis": failure, "remote_task": remote_task,
               "round_started_at": job.get("round_started_at"),
               "budget_started_at": budget_start,
               "preserved_sha256": preserved,
               "reason": "显式升级本轮请求指导契约；原编译 Skill、来源、请求、历史结果及旧修订次数保持不变"}
    if apply:
        _write_json(receipt_path, receipt)
    return receipt


def _authorization(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("action") != "retry_with_new_budget":
        raise ValueError("人工重试必须明确选择一次新尝试预算")
    try:
        request_id = str(uuid.UUID(str(value.get("request_id") or "")))
    except ValueError as exc:
        raise ValueError("人工重试的 request_id 必须是 UUID") from exc
    if type(value.get("budget_seconds")) is not int or not str(value.get("authorized_by") or "").strip():
        raise ValueError("人工重试必须记录明确预算和授权管理员")
    return {key: value.get(key) for key in ("action", "expected_task_id", "budget_seconds", "authorized_by")} | {
        "request_id": request_id}


def explicit_retry_matches(job: dict, authorization: dict) -> bool:
    retry = job.get("native_explicit_retry")
    if not retry:
        return False
    if retry.get("authorization") != _authorization(authorization):
        raise ValueError("此任务已使用一次人工重试授权，不能再次授予新预算")
    return True


def prepare_explicit_retry(root: Path, job: dict, *, authorization: dict | None = None,
                           apply: bool = False, remote_task: dict | None = None) -> dict:
    """An administrator may authorize exactly one separately budgeted test of a format fix."""
    from .native_compiler import record_format_guidance, request_guidance, retry_review_guidance
    from .ov_compile_mining import utc_now
    from .ov_round_orchestration import _digest, _write_json

    if (job.get("execution_protocol") != PROTOCOL or job.get("status") != "failed"
            or job.get("ov_status") not in {"failed", "completed", "succeeded"}
            or job.get("ov_cancel_reason") or job.get("native_explicit_retry")):
        raise ValueError("任务不在可授权一次人工格式重试的失败边界")
    failure = _failure(job)
    if failure["kind"] != "output_protocol":
        raise ValueError("一次新预算仅适用于明确的输出格式协议失败")
    assets, request = _frozen_request(root, job)
    from .native_retry_baseline import baseline_plan
    baseline = baseline_plan(root, job)
    format_guidance = record_format_guidance()
    format_sha = _digest(format_guidance)
    if not format_guidance.strip() or format_guidance.strip() in str(request.get("instruction") or ""):
        raise ValueError("本次格式指导已执行过或尚未提供实际修复")
    for row in job.get("native_compile_history", []):
        path = root / "round_inputs" / f"round-{row['round']}" / "native-requests" / (
            _digest(row["submission_key"]) + ".json")
        _sha(root, path)
        if format_guidance.strip() in str(json.loads(path.read_text(encoding="utf-8")).get("instruction") or ""):
            raise ValueError("此任务已经使用本次格式指导，不能重复授予预算")
    seconds = int(job.get("runtime_timeout_seconds") or 3600)
    context = {"expected_task_id": job["ov_task_id"], "budget_seconds": seconds,
               "format_guidance_sha256": format_sha}
    if authorization is None:
        if apply:
            raise ValueError("必须先明确记录人工重试授权")
        return context
    auth = _authorization(authorization)
    if auth["expected_task_id"] != context["expected_task_id"] or auth["budget_seconds"] != seconds:
        raise ValueError("人工重试必须匹配当前失败任务，预算必须等于原任务运行期限")
    if apply:
        _verify_terminal(job, failure, remote_task)
    number = int(job["current_round"])
    guidance = request_guidance(assets) + format_guidance + retry_review_guidance()
    key = f"te-sm-{job['job_id']}-retry-{_digest(job['ov_task_id'] + auth['request_id'] + format_sha)[:24]}"
    expected = {"protocol": EXPLICIT_RETRY_PROTOCOL, "job_id": job["job_id"], "round": number,
                "request": request, "error": job["error"], "failed_at": job["finished_at"],
                "guidance": guidance, "format_guidance": format_guidance, "authorization": auth,
                "task_id": job["ov_task_id"], "submission_key": key, "baseline_plan": baseline}
    folder = root / "round_inputs" / f"round-{number}" / "native-explicit-retry"
    path = folder / f"{auth['request_id']}.json"
    existing = list(folder.glob("*.json"))
    if existing:
        if existing != [path]:
            raise ValueError("已记录一次人工重试授权，不能更换请求标识再授权")
        _sha(root, path)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if any(receipt.get(field) != value for field, value in expected.items()):
            raise ValueError("已冻结的人工授权、原请求或格式指导发生变化")
        _verify_preserved(root, receipt["preserved_sha256"])
        return receipt
    now = utc_now()
    start = datetime.fromisoformat(now)
    old_start = str(job.get("round_started_at") or job.get("started_at") or "")
    old_seconds = int(job.get("round_timeout_seconds") or seconds)
    old_time = datetime.fromisoformat(old_start.replace("Z", "+00:00"))
    if old_time.tzinfo is None:
        old_time = old_time.replace(tzinfo=timezone.utc)
    retry = {"protocol": EXPLICIT_RETRY_PROTOCOL, "round": number, "authorization": auth,
             "authorized_at": now, "previous_task_id": job["ov_task_id"], "submission_key": key,
             "guidance_sha256": _digest(guidance), "format_guidance_sha256": format_sha,
             "old_budget": {"started_at": old_start, "seconds": old_seconds,
                            "deadline_at": (old_time + timedelta(seconds=old_seconds)).isoformat()},
             "attempt_budget": {"started_at": now, "seconds": seconds,
                                "deadline_at": (start + timedelta(seconds=seconds)).isoformat()}}
    preserved = {p.relative_to(root).as_posix(): _sha(root, p) for p in sorted(root.rglob("*"))
                 if p.is_file() and p not in {root / "job.json", root / "run.log"}}
    receipt = {**expected, "retry": retry, "diagnosis": failure, "remote_task": remote_task,
               "round_started_at": job.get("round_started_at"), "preserved_sha256": preserved}
    if apply:
        _write_json(path, receipt)
    return receipt
