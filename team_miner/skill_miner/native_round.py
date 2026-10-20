"""One immutable native Compile submission per round, with a stable incremental target."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.exceptions import HTTPException

from .mining_intent import intent_from_job
from .native_compiler import METHOD_SCHEMA, NOTES_SCHEMA, PROTOCOL
from .native_components import SOURCE_RESOURCE_PROTOCOL, SUPPORTED_PROTOCOLS, assemble_components
from .native_source_resources import frozen_source_resources
from .output_language import (
    output_language_instruction,
    resolve_output_language_policy,
    validate_artifact_headings,
)


class NativeArtifactError(ValueError):
    """A completed Compile has invalid artifacts; retain them without resubmitting."""


def _check_native_submission(root: Path, job: dict) -> None:
    """A new POST needs an unused round key or a separately authorized frozen attempt."""
    number, key = int(job["current_round"]), job["submission_idempotency_key"]
    accepted = [row for row in job.get("native_compile_history", []) if row["round"] == number]
    if any(row["submission_key"] == key for row in accepted):
        raise ValueError("本轮已有此标识的 Native 受理记录；远端对账缺失时不能重新提交")
    if not accepted and key == f"te-sm-{job['job_id']}-round-{number}":
        return
    attempts = [job.get("native_explicit_retry") or {}, *(job.get("native_contract_recoveries") or [])]
    if not any(row.get("round") == number and row.get("submission_key") == key for row in attempts):
        raise ValueError("本轮仅一次 Compile；额外受理必须有独立人工尝试的冻结授权，不能自动换标识重编译")
    from .native_recovery import pending_guidance
    if not pending_guidance(root, job, ""):
        raise ValueError("额外 Native 尝试缺少可核验的冻结授权记录")


def _reviewed_method_catalog(root: Path, reviewed: Path, previous_round: int) -> str:
    """Carry prior method identities and boundaries, without bodies, Cases or new Evidence."""
    methods = []
    fields = ("name", "task", "trigger", "inputs", "outputs", "boundary", "rationale", "source_refs", "supersedes")
    for path in sorted((reviewed / "compiled_skill").glob("*/METHOD.json")):
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError("上一轮方法目录要求任务内的普通归档文件")
        raw = path.read_bytes()
        record = json.loads(raw)
        if (not isinstance(record, dict) or record.get("name") != path.parent.name
                or any(key not in record for key in fields[:-1])):
            raise ValueError("上一轮方法目录缺少完整身份与边界，不能推测既有方法")
        methods.append({**{key: record[key] for key in fields if key in record},
                        "method_path": path.relative_to(reviewed).as_posix(),
                        "method_sha256": hashlib.sha256(raw).hexdigest()})
    return json.dumps({"schema_version": "te-native-method-catalog.v1", "round": previous_round,
                       "is_evidence": False, "requires_source_review": True,
                       "note": "上一轮已复核产物的身份与边界目录；不是新增事实，旧方法仍须依据本轮原始来源核验。",
                       "methods": methods}, ensure_ascii=False, separators=(",", ":"))


def _reviewed_component_catalog(
    root: Path, reviewed: Path, previous_round: int, allowed_sources: set[str], *, required: bool = False,
    frozen_sources: dict[str, bytes] | None = None,
) -> str | None:
    """Carry validated component ownership, never bodies or new Evidence, into a delta round."""
    import tempfile

    from .native_components import _files, _load_json, _path, assemble_components

    if reviewed.is_symlink() or root.resolve() not in reviewed.resolve().parents:
        raise ValueError("上一轮组件目录要求任务内的普通归档目录")
    methods = sorted((reviewed / "compiled_skill").glob("*/METHOD.json"))
    if not methods:
        return None
    reports = [name for name in ("semantic_reports", "analysis")
               if (reviewed / name / "components").exists()]
    if len(reports) > 1:
        raise ValueError("上一轮组件目录存在不明确的重复归档")
    report_directory = reports[0] if reports else "semantic_reports"
    component_names = {path.parent.name for path in methods
                       if (reviewed / report_directory / "components" / path.parent.name / "manifest.json").is_file()}
    if not component_names:
        if required:
            raise ValueError("上一轮组件协议方法缺少完整组件清单，不能猜测旧路径或 Case ID")
        return None
    if component_names != {path.parent.name for path in methods}:
        raise ValueError("上一轮组件清单未覆盖全部当前方法，不能部分猜测增量分工")

    def local_path(remote_relative: str) -> Path:
        relative = (report_directory + remote_relative[len("analysis"):]
                    if remote_relative.startswith("analysis/") else remote_relative)
        return _path(reviewed.resolve(), relative)

    # Reuse the actual acceptance contract in an isolated copy, rather than
    # duplicating its manifest/schema/source checks or mutating reviewed files.
    with tempfile.TemporaryDirectory(prefix="te-native-component-catalog-") as scratch:
        validation = Path(scratch)
        for method in methods:
            name = method.parent.name
            for relative in (f"compiled_skill/{name}", f"analysis/components/{name}"):
                source = local_path(relative)
                _files(reviewed.resolve(), source.relative_to(reviewed.resolve()).as_posix())
                destination = validation / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination, symlinks=True)
            # These are local projections, not native remote components. Human
            # edits still travel through the existing revision-difference input.
            for relative in (f"compiled_skill/{name}/benchmark.json", f"compiled_skill/{name}/BENCHMARK.md",
                             f"analysis/components/{name}/assembly-audit.json"):
                derived = validation / relative
                if derived.exists():
                    derived.unlink()
        audits = assemble_components(validation, allowed_sources, required=True, frozen_sources=frozen_sources)
        packages = []
        for audit in audits or []:
            name = audit["name"]
            manifest_path = f"analysis/components/{name}/manifest.json"
            files = []
            manifest_bytes = b""
            for entry in audit["inputs"]:
                actual = local_path(entry["path"]).read_bytes()
                if (len(actual) != entry["bytes"]
                        or hashlib.sha256(actual).hexdigest() != entry["sha256"]):
                    raise ValueError("上一轮组件文件在目录核对期间变化")
                files.append(dict(entry))
                if entry["path"] == manifest_path:
                    manifest_bytes = actual
            packages.append({"name": name, "manifest_path": manifest_path,
                             "manifest": _load_json(manifest_bytes, manifest_path),
                             "files": files})
    return json.dumps({
        "schema_version": "te-native-component-catalog.v1", "round": previous_round,
        "is_evidence": False, "requires_source_review": True,
        "note": "上一轮已复核组件的完整分工与实际文件摘要；path相对同一Compile目标。"
                "这是宿主复制的修订上下文，不是人工新增要求或Evidence，也不是远端当前版本证明。"
                "保留未受影响组件、稳定路径与Case ID；历史正文仍须按当前单元targets召回。"
                "本地派生benchmark.json、BENCHMARK.md与装配审计不属于远端组件。",
        "methods": packages,
    }, ensure_ascii=False, separators=(",", ":"))


def _validate_display(value, policy: dict) -> None:
    """Check human fields, leaving machine identifiers, enums and quotations intact."""
    if policy.get("language") != "zh-CN":
        return
    if isinstance(value, str):
        if value.strip() and re.search(r"[A-Za-z]", value) and not re.search(r"[\u3400-\u9fff]", value):
            raise ValueError("面向人的正文、名称和评测说明必须使用中文")
    elif isinstance(value, list):
        for row in value:
            _validate_display(row, policy)
    elif isinstance(value, dict):
        for key, row in value.items():
            if key in {"name", "title", "description", "query", "text", "task", "trigger", "inputs",
                       "outputs", "boundary", "rationale", "question", "reason"}:
                _validate_display(row, policy)
            elif isinstance(row, dict):
                _validate_display(row, policy)
            elif isinstance(row, list):
                for item in row:
                    if isinstance(item, dict):
                        _validate_display(item, policy)


def _project_output(repository, job: dict, raw: Path, destination: Path) -> None:
    """Assemble only model-authored business data; never infer semantic membership."""
    from .mining_completion import completion_only, project_output
    if completion_only(job):
        project_output(raw, destination)
        return
    from .dataset_view import dataset_markdown
    from .ov_compile_mining import PLAN_SCHEMA_VERSION, REVIEW_SCHEMA_VERSION

    allowed = set(job["native_allowed_source_uris"])
    policy = job["output_language_policy"]
    if any(path.is_symlink() for path in raw.rglob("*")):
        raise ValueError("原生产物不能包含符号链接")
    shutil.copytree(raw, destination)
    (destination / "compiled_skill").mkdir(exist_ok=True)
    reports = destination / "analysis"
    reports.mkdir(exist_ok=True)
    component_audits = assemble_components(
        destination, allowed, required=job.get("native_component_protocol") in SUPPORTED_PROTOCOLS,
        frozen_sources=(frozen_source_resources(repository.jobs_root / job["job_id"], job, int(job["current_round"]))
                        if job.get("native_component_protocol") == SOURCE_RESOURCE_PROTOCOL else None),
    )
    copied_resources = {row["path"] for audit in component_audits or [] for row in audit.get("source_copies", [])}
    plan, deferred, questions, supersedes = [], [], {}, {}

    def check_refs(refs):
        if not isinstance(refs, list) or not refs or any(ref not in allowed for ref in refs):
            raise ValueError("原生编译引用必须是本任务已冻结的原始来源或补证文件")

    def add_questions(rows):
        if not isinstance(rows, list):
            raise ValueError("原生编译补证问题必须为数组")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("原生编译补证问题必须为对象")
            check_refs(row.get("source_refs"))
            qid = row.get("qid")
            if not isinstance(qid, str) or (qid in questions and questions[qid] != row):
                raise ValueError("原生编译补证问题标识无效或冲突")
            questions[qid] = row

    for method in sorted((destination / "compiled_skill").glob("*/METHOD.json")):
        record = json.loads(method.read_text(encoding="utf-8"))
        fields = {"schema_version", "name", "task", "trigger", "inputs", "outputs", "boundary",
                  "rationale", "source_refs", "questions"}
        if (not isinstance(record, dict) or set(record) - {"supersedes"} != fields
                or record["schema_version"] != METHOD_SCHEMA):
            actual = set(record) if isinstance(record, dict) else set()
            raise ValueError(
                f"METHOD.json字段无效，缺少{sorted(fields - actual)}，"
                f"多余{sorted(actual - fields - {'supersedes'})}"
            )
        if record["name"] != method.parent.name:
            raise ValueError("原生编译的方法标识必须与目录一致")
        check_refs(record["source_refs"])
        for key in ("inputs", "outputs"):
            if not isinstance(record[key], list) or not all(isinstance(value, str) for value in record[key]):
                raise ValueError("方法输入输出必须为文字数组")
        replaced = record.get("supersedes", [])
        if not isinstance(replaced, list) or any(not isinstance(name, str) for name in replaced):
            raise ValueError("方法替代关系必须为明确的既有标识数组")
        supersedes[record["name"]] = replaced
        row = {key: value for key, value in record.items()
               if key not in {"schema_version", "questions", "supersedes"}}
        _validate_display({key: value for key, value in row.items() if key != "name"}, policy)
        plan.append(row)
        add_questions(record["questions"])
        candidate = method.parent
        dataset = json.loads((candidate / "benchmark.json").read_text(encoding="utf-8"))
        if len(dataset.get("cases", [])) != 12:
            raise ValueError("原生编译的每个方法必须交付12个完整案例")
        queries = [re.sub(r"\s+", " ", str(case.get("query") or "")).strip() for case in dataset["cases"]]
        if len(set(queries)) != 12:
            raise ValueError("原生编译的12个案例不能重复相同Query")
        for case in dataset["cases"]:
            check_refs((case.get("provenance") or {}).get("session_ids"))
        _validate_display(dataset, policy)
        for filename in ("SKILL.md", "EVALUATION.md"):
            body = (candidate / filename).read_text(encoding="utf-8")
            validate_artifact_headings(body, policy, machine_name=record["name"])
            _validate_display(body, policy)
        for resource in sorted((candidate / "references").rglob("*")) if component_audits else []:
            # Verified quotations preserve their source language and bytes;
            # every model-authored resource still follows the output policy.
            if (resource.is_file() and resource.suffix == ".md"
                    and resource.relative_to(destination).as_posix() not in copied_resources):
                body = resource.read_text(encoding="utf-8")
                validate_artifact_headings(body, policy, machine_name=record["name"])
                _validate_display(body, policy)
        (candidate / "BENCHMARK.md").write_text(dataset_markdown(dataset), encoding="utf-8")

    notes = list(reports.glob("notes/*.json"))
    for path in notes:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or set(record) != {"schema_version", "deferred", "questions"}
                or record["schema_version"] != NOTES_SCHEMA or not isinstance(record["deferred"], list)):
            raise ValueError("原生编译的非方法处置契约无效")
        for row in record["deferred"]:
            check_refs(row.get("source_refs") if isinstance(row, dict) else None)
            deferred.append(row)
        add_questions(record["questions"])
        _validate_display(record, policy)
    if not plan and not deferred:
        raise ValueError("原生编译没有交付完整方法或有来源的内容处置说明")
    actual = {path.name for path in (destination / "compiled_skill").iterdir() if path.is_dir()}
    if actual != {row["name"] for row in plan}:
        raise ValueError("原生编译存在缺少方法回执的不完整候选")
    retired = set()
    for name, replaced in supersedes.items():
        if len(set(replaced)) != len(replaced) or any(other not in actual or other == name for other in replaced):
            raise ValueError("方法替代关系必须引用不同的完整既有成员")
        if retired.intersection(replaced):
            raise ValueError("一个既有方法不能被多个方法同时替代")
        retired.update(replaced)
    if any(name in retired and any(other not in retired for other in replaced)
           for name, replaced in supersedes.items()):
        raise ValueError("方法替代关系存在循环")
    # A closed replacement cycle has no survivor; detect all cycles explicitly.
    def visit(name, ancestors):
        if name in ancestors:
            raise ValueError("方法替代关系存在循环")
        for other in supersedes[name]:
            visit(other, ancestors | {name})
    for name in supersedes:
        visit(name, set())
    plan = [row for row in plan if row["name"] not in retired]
    for name in sorted(retired):
        lineage = reports / "lineage" / name
        lineage.parent.mkdir(exist_ok=True)
        shutil.move(destination / "compiled_skill" / name, lineage)
    (reports / "capability-plan.json").write_text(json.dumps({
        "schema_version": PLAN_SCHEMA_VERSION, "skills": plan, "deferred": deferred,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports / "review.json").write_text(json.dumps({
        "schema_version": REVIEW_SCHEMA_VERSION, "questions": list(questions.values()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence = "# 来源对应\n\n" + "\n\n".join(
        f"## {row['task']}\n\n" + "\n".join(f"- {ref}" for ref in row["source_refs"]) for row in plan
    )
    gaps = "# 内容处置与未解决问题\n\n" + "\n\n".join(
        [row["reason"] for row in deferred] + [row["question"] for row in questions.values()]
    )
    (reports / "evidence-map.md").write_text(evidence, encoding="utf-8")
    (reports / "gaps.md").write_text(gaps, encoding="utf-8")
    reports.rename(destination / "semantic_reports")
    preview = {**job, "workspace": destination.relative_to(repository.project_root).as_posix()}
    repository._read_capability_plan(preview)
    repository._read_review(preview)
    for row in plan:
        repository._validate_candidate(preview, destination / "compiled_skill" / row["name"])
    intent = intent_from_job(job)
    if intent["target_count"] is not None and len(plan) > intent["target_count"]:
        raise ValueError("原生编译交付的方法数量超过用户指定目标")


class NativeRoundMixin:
    async def _resume_native_resource_failure(self, job_id: str, authorization: dict | None = None) -> dict:
        repository = self._ov_skill_mining_repository()
        async with self._ov_skill_mining_lock(job_id):
            job = await asyncio.to_thread(repository.get_job, job_id)
            from .native_recovery import explicit_retry_matches, prepare_explicit_retry, prepare_recovery
            if authorization is not None and explicit_retry_matches(job, authorization):
                return job
            if (authorization is None and (job.get("native_resource_recovery") or job.get("native_contract_recoveries"))
                    and job.get("status") in {"preparing", "queued", "running"}):
                return job
            prepare = prepare_recovery if authorization is None else prepare_explicit_retry
            await asyncio.to_thread(prepare, repository.jobs_root / job_id, job,
                                    **({"authorization": authorization} if authorization is not None else {}))
            task = await self._knowledge_request(job["account"], "GET", f"/api/v1/tasks/{job['ov_task_id']}",
                timeout=10.0, params={"include_events": "true"})
            if task.get("task_id", job["ov_task_id"]) != job["ov_task_id"]:
                raise ValueError("远端返回的 Native 身份与恢复任务不一致")
            return await asyncio.to_thread(repository.resume_native_failure, job_id,
                                           {**task, "task_id": job["ov_task_id"]}, authorization=authorization)

    async def _native_admit(self, job: dict) -> bool:
        from .compile_admission import OPTIMIZED_PROTOCOL
        root = self._ov_skill_mining_repository().jobs_root / job["job_id"]
        await asyncio.to_thread(_check_native_submission, root, job)
        admitted = await self._acquire_staged_compile_slot(
            job, {"execution_protocol": OPTIMIZED_PROTOCOL}, {"key": job["submission_idempotency_key"]},
        )
        if not admitted:
            await asyncio.to_thread(self._ov_skill_mining_repository().update_job, job["job_id"],
                ov_next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat())
        return admitted

    async def _native_inputs(self, job: dict) -> list[dict]:
        """Freeze only this review's delta plus originals explicitly implicated by its questions."""
        from .ov_compile_mining import RUNS_ROOT
        from .ov_round_orchestration import _cache_inputs, _cached_inputs

        root = self._ov_skill_mining_repository().jobs_root / job["job_id"]
        number = int(job["current_round"])
        if number == 1:
            return await self._freeze_round_inputs(job)
        cache = root / "round_inputs" / f"round-{number}"
        frozen = await asyncio.to_thread(_cached_inputs, cache)
        if frozen is not None:
            return frozen
        record = job.get("last_checkpoint_submission") or {}
        if record.get("round") != number - 1:
            raise ValueError("增量编译需要紧邻上一轮的真实复核记录")
        original = await asyncio.to_thread(_cached_inputs, root / "round_inputs/round-1")
        if not original:
            raise ValueError("缺少第一轮冻结原文")
        files = []
        # User answers are assertions; questions, feedback and unresolved items remain labelled context.
        human = {key: record.get(key) for key in (
            "round", "answers", "unresolved", "questions", "feedback", "submitted_by", "submitted_at",
        )}
        files.append({"kind": "human", "relative_path": "supplements/review.md", "content": (
            "# 本轮人工补证与修订要求\n\n仅 answers 中的实际回答是用户断言；questions、feedback 是修订上下文，"
            "unresolved 尚未解决，禁止将问题当成事实。\n\n" + json.dumps(human, ensure_ascii=False, indent=2)
        )})
        refs = {uri for question in record.get("questions", []) for uri in question.get("source_refs", [])}
        original_aliases = {}
        for previous_number in range(1, number):
            preceding = await asyncio.to_thread(_cached_inputs, root / "round_inputs" / f"round-{previous_number}")
            if preceding is None:
                raise ValueError("缺少已归档轮次的冻结来源")
            for item in preceding:
                if item["kind"] == "source":
                    aliases = original_aliases.setdefault(item["relative_path"], set())
                    aliases.add(f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{previous_number}/source/"
                                + item["relative_path"])
                    if item.get("origin_uri"):
                        aliases.add(item["origin_uri"])
        # Unknown impact (freeform feedback) conservatively makes the complete original available.
        for item in original:
            original_uri = job["original_source_snapshot"] + "/" + item["relative_path"]
            aliases = original_aliases.get(item["relative_path"], set())
            implicated = bool(refs.intersection(aliases | {
                item.get("origin_uri"), original_uri, job["source_uri"], job["original_source_snapshot"],
            }))
            if item["kind"] == "source" and (implicated or bool(record.get("feedback"))):
                files.append({**{key: item[key] for key in ("kind", "relative_path", "origin_uri")},
                              "content": await asyncio.to_thread(Path(item["local_path"]).read_text, encoding="utf-8")})
        for attachment in record.get("attachments", []) + record.get("reference_snapshots", []):
            path = Path(attachment["local_path"])
            if path.is_symlink() or root.resolve() not in path.resolve().parents:
                raise ValueError("增量补证附件路径越界")
            from .ov_round_orchestration import _digest
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if _digest(content) != attachment.get("sha256"):
                raise ValueError("增量补证附件已被修改")
            name = attachment.get("name") or _digest(str(path)) + ".md"
            files.append({"kind": "human", "relative_path": "supplements/" + name, "content": content})
        reviewed = root / "rounds" / f"round-{number - 1}" / "reviewed_output"
        previous = root / "rounds" / f"round-{number - 1}" / "output"
        if not reviewed.is_dir() or not previous.is_dir():
            raise ValueError("缺少上一轮及人工复核后的版本归档")
        from .mining_completion import completion_only, revision_files
        if completion_only(job):
            files.extend(await asyncio.to_thread(revision_files, root, reviewed))
            return await asyncio.to_thread(_cache_inputs, cache, files)
        catalog = await asyncio.to_thread(_reviewed_method_catalog, root, reviewed, number - 1)
        files.append({"kind": "context", "relative_path": "revision/method-catalog.json", "content": catalog})
        components = await asyncio.to_thread(
            _reviewed_component_catalog, root, reviewed, number - 1,
            set(job.get("native_allowed_source_uris") or []),
            required=job.get("native_component_protocol") in SUPPORTED_PROTOCOLS,
            frozen_sources=(await asyncio.to_thread(frozen_source_resources, root, job, number - 1)
                            if job.get("native_component_protocol") == SOURCE_RESOURCE_PROTOCOL else None),
        )
        if components is not None:
            files.append({"kind": "context", "relative_path": "revision/component-catalog.json", "content": components})
        for path in sorted(reviewed.rglob("*")):
            if path.is_file():
                relative = path.relative_to(reviewed)
                baseline = previous / relative
                if not baseline.is_file() or await asyncio.to_thread(path.read_bytes) != await asyncio.to_thread(
                    baseline.read_bytes
                ):
                    files.append({"kind": "context", "relative_path": "revision/" + relative.as_posix(),
                                  "content": await asyncio.to_thread(path.read_text, encoding="utf-8")})
        return await asyncio.to_thread(_cache_inputs, cache, files)

    async def _submit_native_round(self, job: dict, client, started: float) -> dict:
        from .ov_compile_mining import RUNS_ROOT, utc_now
        from .ov_round_orchestration import _cached_inputs, _compiler_package, _digest

        repository = self._ov_skill_mining_repository()
        root = repository.jobs_root / job["job_id"]
        number = int(job["current_round"])
        await asyncio.to_thread(_check_native_submission, root, job)
        compiler = await asyncio.to_thread((root / "compiler-skill.md").read_text, encoding="utf-8")
        path = root / f"compiler-round-{number}.md"
        if path.exists() and await asyncio.to_thread(path.read_text, encoding="utf-8") != compiler:
            raise ValueError("本轮冻结的编译 Skill 已改变")
        if not path.exists():
            await asyncio.to_thread(path.write_text, compiler, encoding="utf-8")
        if PROTOCOL not in compiler:
            raise ValueError("原生编译任务缺少冻结的编译 Skill")
        name, published, source_sha = _compiler_package(compiler)
        from .native_compiler import frozen_assets, request_guidance
        assets = await asyncio.to_thread(frozen_assets, root, compiler)
        provisioned = await self._round_provision_compiler(client, name, published, assets)
        from .native_recovery import pending_guidance
        guidance = await asyncio.to_thread(pending_guidance, root, job,
                                           request_guidance(assets) if assets is not None else "")
        entries = await self._native_inputs(job)
        policy = job.get("output_language_policy") or await asyncio.to_thread(resolve_output_language_policy, entries)
        source = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{number}/source"
        directories = {source, f"{RUNS_ROOT}/{job['job_id']}", f"{RUNS_ROOT}/{job['job_id']}/rounds",
                       source.rsplit("/", 1)[0]}
        for item in entries:
            directory = (source + "/" + item["relative_path"]).rsplit("/", 1)[0]
            while directory.startswith(source):
                directories.add(directory)
                directory = directory.rsplit("/", 1)[0]
        for directory in sorted(directories, key=lambda uri: (uri.count("/"), uri)):
            await client.mkdir(directory)
        allowed = set(job.get("native_allowed_source_uris", []))
        for item in entries:
            uri = source + "/" + item["relative_path"]
            content = await asyncio.to_thread(Path(item["local_path"]).read_text, encoding="utf-8")
            await self._round_write_immutable(job["account"], uri, content)
            if item["kind"] != "context":
                allowed.add(uri)
                if item.get("origin_uri"):
                    allowed.add(item["origin_uri"])
        # Validate all earlier local inputs before reusing their citation identities.
        for previous in range(1, number):
            await asyncio.to_thread(_cached_inputs, root / "round_inputs" / f"round-{previous}")
        instruction = (
            f"任务：{job['name']}。第{number}轮，轮数上限{job['max_rounds']}。"
            "一次 Compile 完成本轮全部方法和配套文件，不发布。"
            "同一个 to 是本任务增量工作区，已有生成内容仅作修订上下文。"
            "保留未受影响方法、原条件和Case ID。问题不能当答案，未解决事实继续报告。"
            "只生成本次用户目标支持的完整方法。\nmining_intent="
            + json.dumps(intent_from_job(job), ensure_ascii=False)
            + output_language_instruction(policy)
            + "\n原文与补证引用仅允许已冻结的文件URI；禁止引用产物作为Evidence。"
            + "revision/下文件是人工编辑的产物上下文，按其明确修改要求修订，不能作为事实来源。"
            + ("\n其中 revision/method-catalog.json 由上一轮已复核 METHOD.json 确定性复制，"
               "仅是旧身份与边界索引，不是人工新增要求或 Evidence；"
               "用于判断影响范围，不表示本轮必须重写目录中的所有方法。"
               if any(item["kind"] == "context" and item["relative_path"] == "revision/method-catalog.json"
                      for item in entries) else "")
            + ("\nrevision/component-catalog.json 同样是从上一轮已复核产物确定性复制的组件目录与摘要，"
               "包含原有文件路径、CaseID和来源声明；不是人工新增要求或Evidence。"
               "据此保留既有组件身份，全文仍从本次提供的历史target召回。"
               if any(item["kind"] == "context" and item["relative_path"] == "revision/component-catalog.json"
                      for item in entries) else "")
            + str(job.get("native_repair_instruction") or "")
            + guidance
        )
        from .mining_completion import completion_only
        if completion_only(job):
            instruction = (
                f"任务：{job['name']}。第{number}轮。按冻结编译 Skill 的业务边界生成独立 Skill 与配套资源。"
                "本轮一次 Compile；有界分工、文件分段写入，不生成 benchmark 或验证案例。"
                "只记录挖掘完成状态，不要求内容验收；未决关系如实写入产物。\nmining_intent="
                + json.dumps(intent_from_job(job), ensure_ascii=False)
                + output_language_instruction(policy)
                + "\n原始来源和实际补证才是 Evidence，已有产物只是修订上下文。"
                + guidance
            )
            if number > 1:
                instruction += (
                    "\n本轮是对同一任务既有产物的修订，revision/下文件是上一轮原始返回内容，"
                    "只作修订上下文，不作为新增事实。按本轮明确反馈修改涉及的文件，保留其他有效内容、"
                    "稳定目录和资源引用；不要将旧产物再次当成知识点来源。修订要求如下：\n"
                    + str((job.get("last_checkpoint_submission") or {}).get("feedback") or "")
                )
        payload = {"from": [source], "to": job["stable_output_uri"],
                   "skill": f"viking://agent/skills/{name}", "instruction": instruction}
        key = job["submission_idempotency_key"]
        from .ov_round_orchestration import _write_json
        request_archive = root / "round_inputs" / f"round-{number}" / "native-requests" / f"{_digest(key)}.json"
        if request_archive.is_file():
            if json.loads(await asyncio.to_thread(request_archive.read_text, encoding="utf-8")) != payload:
                raise ValueError("原生Compile冻结请求已改变")
        else:
            await asyncio.to_thread(_write_json, request_archive, payload)
        job = await asyncio.to_thread(repository.update_job, job["job_id"], compile_request=payload,
            source_snapshot=source, original_source_snapshot=job.get("original_source_snapshot") or source,
            output_uri=job["stable_output_uri"], compiler_path=str(path), compiler_uri=payload["skill"],
            skill_uri=payload["skill"], compiler_source_sha256=source_sha, compiler_sha256=_digest(published),
            native_allowed_source_uris=sorted(allowed), output_language_policy=policy,
            native_component_protocol=next((protocol for protocol in sorted(SUPPORTED_PROTOCOLS)
                                            if f"component_protocol: {protocol}" in compiler.splitlines()), None),
            input_manifest=str(root / "round_inputs" / f"round-{number}" / "manifest.json"),
            round_started_at=job.get("round_started_at") or utc_now(),
            submission_started_at=job.get("submission_started_at") or utc_now())
        from .native_recovery import ensure_round_budget
        ensure_round_budget(job)
        if not await self._native_admit(job):
            return await asyncio.to_thread(repository.get_job, job["job_id"])
        try:
            ensure_round_budget(job)
        except ValueError:
            # This fresh path has not attempted POST; releasing its reservation is safe.
            await self._release_staged_compile_slot(job, key=key)
            raise
        from .native_retry_baseline import restore_baseline
        try:
            existing = await restore_baseline(self, job)
            if existing is None:
                ensure_round_budget(job)
        except (ValueError, OSError):
            # Neither baseline validation nor restoration submits Compile on this fresh path.
            await self._release_staged_compile_slot(job, key=key)
            raise
        if existing is not None:
            return await self._record_round_acceptance(repository, job, existing, started)
        try:
            accepted = await self._knowledge_request(job["account"], "POST", "/api/v1/compile", timeout=60.0,
                extra_headers={"Idempotency-Key": key}, json=payload)
        except HTTPException as exc:
            # Definitive validation/auth rejection cannot own a remote task. A lost
            # response, 5xx or idempotency conflict keeps its durable reservation.
            if exc.status_code in {400, 401, 403, 413, 422}:
                await self._release_staged_compile_slot(job, key=key)
            raise
        return await self._record_round_acceptance(repository, job, accepted, started,
            str((provisioned.get("result") or {}).get("revision") or ""))

    async def _repair_native_round(self, job: dict, error: str, *, task: dict | None = None,
                                   local_validation: bool = False) -> dict:
        """Record the terminal failure; one round never starts another Compile automatically."""
        from .native_failures import classify_native_failure
        from .ov_compile_mining import utc_now
        from .ov_round_orchestration import _write_json

        repository = self._ov_skill_mining_repository()
        observed = dict(task or {})
        observed.setdefault("task_id", job.get("ov_task_id") or "")
        observed.setdefault("error", error)
        failure = classify_native_failure(observed, compiler_uri=str(job.get("compiler_uri") or ""),
            source_uris=[*(job.get("native_allowed_source_uris") or []), job.get("source_snapshot"),
                         job.get("source_uri"), job.get("original_source_snapshot")],
            local_validation_detail=error if local_validation else None)
        failure_root = (repository.jobs_root / job["job_id"] / "round_inputs" / f"round-{job['current_round']}"
                        / "native-failures")
        failure_path = failure_root / f"{failure['fingerprint']}.json"
        # A terminal observation is immutable, including its real error and execution events.
        if failure_path.exists():
            if failure_path.is_symlink() or json.loads(await asyncio.to_thread(
                failure_path.read_text, encoding="utf-8"
            )) != failure:
                raise ValueError("原生编译失败诊断归档已改变")
        else:
            await asyncio.to_thread(_write_json, failure_path, failure)
        failure["archive"] = str(failure_path.relative_to(repository.project_root))
        return await asyncio.to_thread(repository.update_job, job["job_id"], status="failed",
            native_failure=failure, native_repair_instruction="",
            error=f"原生编译失败（{failure['kind']}，本轮仅一次 Compile，未自动重编译）：{error}",
            finished_at=utc_now())

    async def _materialize_native_round(self, job: dict) -> dict:
        from .content_io import _content_text
        from .mining_completion import completion_only
        from .ov_compile_mining import validate_descendant

        repository = self._ov_skill_mining_repository()
        root = repository.jobs_root / job["job_id"]
        cache = root / "round_outputs" / f"round-{job['current_round']}"
        attempt = uuid.uuid4().hex
        raw, preview = cache / f"raw-{attempt}", cache / f"projected-{attempt}"
        raw.mkdir(parents=True, exist_ok=True)
        count, total = 0, 0
        async for entry in self._iter_knowledge_entries(job["account"], job["output_uri"]):
            if entry.get("is_dir"):
                continue
            uri = validate_descendant(job["output_uri"], entry.get("uri"), allow_root=False)
            relative = uri[len(job["output_uri"]) + 1:]
            if any(part.startswith(".") for part in relative.split("/")):
                continue
            if not completion_only(job) and not relative.startswith(("analysis/", "compiled_skill/")):
                raise NativeArtifactError("原生编译返回了输出契约之外的文件")
            count += 1
            if count > 10000 or int(entry.get("size") or 0) > 20 * 1024 * 1024:
                raise NativeArtifactError("原生编译产物超过文件数量或单文件上限")
            response = await self._knowledge_request(job["account"], "GET", "/api/v1/content/read",
                params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"})
            content = _content_text(response)
            if len(content.encode("utf-8")) > 20 * 1024 * 1024:
                raise NativeArtifactError("原生编译产物超过单文件上限")
            total += len(content.encode("utf-8"))
            if total > 64 * 1024 * 1024:
                raise NativeArtifactError("原生编译产物超过64MB物化上限")
            path = raw / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        try:
            await asyncio.to_thread(_project_output, repository, job, raw, preview)
        except (ValueError, KeyError, TypeError, UnicodeError, FileNotFoundError) as exc:
            raise NativeArtifactError(str(exc)) from exc
        workspace = repository.workspace(job)
        backup = cache / "previous-workspace"
        if backup.exists():
            if workspace.exists():
                from .ov_compile_mining import _file_sha256
                def same_projection():
                    def hashes(directory):
                        return {str(path.relative_to(directory)): _file_sha256(path)
                                for path in directory.rglob("*") if path.is_file()}
                    return hashes(workspace) == hashes(preview)
                if not await asyncio.to_thread(same_projection):
                    raise ValueError("本轮已有不同的工作区切换结果，拒绝覆盖历史")
            else:
                await asyncio.to_thread(os.replace, preview, workspace)
        else:
            await asyncio.to_thread(os.replace, workspace, backup)
            try:
                await asyncio.to_thread(os.replace, preview, workspace)
            except OSError:
                await asyncio.to_thread(os.replace, backup, workspace)
                raise
        result = await asyncio.to_thread(repository.finalize_materialized, job["job_id"])
        await asyncio.to_thread(repository.append_log, job["job_id"],
            f"原生 Compile 已物化{count}个文件；"
            + ("原始产物已保留，未执行内容验收" if completion_only(job)
               else "方法回执已汇总，Test Dataset视图已确定性生成"))
        return result
