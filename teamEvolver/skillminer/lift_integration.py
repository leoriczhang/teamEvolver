"""SkillMiner → LIFT dataset bridge.

This module intentionally implements only LIFT's public Suite data contract and
invokes a separately configured LIFT checkout.  The upstream repository is not
vendored into teamEvolver: it currently has no license file and brings a separate
Python 3.12 + Docker + Langfuse runtime stack.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).parent.resolve()
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
LIFT_DATASETS_ROOT = Path(
    os.environ.get("TEAMEVOLVER_LIFT_DATASETS_DIR", str(PROJECT_ROOT / "lift_datasets"))
).expanduser().resolve()
DRAFTS_DIR = LIFT_DATASETS_ROOT / "drafts"
PUBLISHED_DIR = LIFT_DATASETS_ROOT / "published"
RUNS_DIR = LIFT_DATASETS_ROOT / "runs"

LIFT_REPOSITORY_URL = "https://github.com/FeiZhuNiU-INFJA/LIFT"
LIFT_COMPATIBILITY_REVISION = "ed8c9d750d729e4c5b1bbf237dd8483d9d142689"
LIFT_SCHEMA = "lift-suite-v1"
DEFAULT_LIFT_ROOT = REPOSITORY_ROOT / "external" / "LIFT"

SUPPORTED_RUNTIMES = (
    "openclaw",
    "openclaw_with_evolve",
    "openclaw_with_openspace",
    "openclaw_with_agentmemory",
    "multi_user_openclaw",
    "genericagent",
    "genericagent_active_evolve",
    "hermes",
    "hermes_with_openspace",
    "hermes_with_agentmemory",
    "openhuman",
    "openhuman_with_agentmemory",
    "evoscientist",
    "evoscientist_active_evolve",
)

_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}
_BUNDLE_DIRS = ("references", "assets", "scripts")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _slug(value: str, fallback: str = "teamEvolver-suite") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return value[:80] or fallback


def _safe_child(root: Path, name: str) -> Path:
    clean_name = str(name or "").strip()
    safe_name = _slug(clean_name)
    if clean_name != safe_name:
        raise ValueError("非法数据集标识")
    candidate = (root / safe_name).resolve()
    root = root.resolve()
    if candidate.parent != root:
        raise ValueError("非法数据集标识")
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{_timestamp()}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def resolve_lift_root() -> Path:
    configured = os.environ.get("TEAMEVOLVER_LIFT_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_LIFT_ROOT.resolve()


def resolve_lift_python() -> str:
    configured = os.environ.get("TEAMEVOLVER_LIFT_PYTHON", "").strip()
    return str(Path(configured).expanduser()) if configured else sys.executable


def _inspect_python(python_bin: str) -> tuple[bool, str]:
    executable = shutil.which(python_bin) or (python_bin if Path(python_bin).is_file() else "")
    if not executable:
        return False, ""
    try:
        check = subprocess.run(
            [executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        version = check.stdout.strip() if check.returncode == 0 else ""
        parts = tuple(int(part) for part in version.split(".")[:2])
        return parts >= (3, 12), version
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, ""


def lift_status() -> dict[str, Any]:
    root = resolve_lift_root()
    python_bin = resolve_lift_python()
    python_ready, python_version = _inspect_python(python_bin)
    markers = [root / "src" / "models.py", root / "src" / "cli" / "lift_main.py"]
    installed_revision = ""
    if (root / ".git").exists() and shutil.which("git"):
        try:
            revision_check = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if revision_check.returncode == 0:
                installed_revision = revision_check.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "repository_url": LIFT_REPOSITORY_URL,
        "compatibility_revision": LIFT_COMPATIBILITY_REVISION,
        "installed_revision": installed_revision,
        "revision_compatible": installed_revision == LIFT_COMPATIBILITY_REVISION,
        "schema": LIFT_SCHEMA,
        "root": str(root),
        "root_configured": bool(os.environ.get("TEAMEVOLVER_LIFT_ROOT", "").strip()),
        "checkout_ready": all(path.is_file() for path in markers),
        "python": python_bin,
        "python_version": python_version,
        "python_ready": python_ready,
        "docker_ready": bool(shutil.which("docker")),
        "datasets_root": str(LIFT_DATASETS_ROOT),
        "supported_runtimes": list(SUPPORTED_RUNTIMES),
    }


def list_source_skills() -> list[dict[str, Any]]:
    base = PROJECT_ROOT / "compiled_skill"
    rows = []
    if not base.is_dir():
        return rows
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        benchmark = skill_dir / "benchmark.jsonl"
        question_count = 0
        if benchmark.is_file():
            lines = benchmark.read_text(encoding="utf-8", errors="ignore").splitlines()
            question_count = sum(1 for line in lines if line.strip())
        rows.append({
            "name": skill_dir.name,
            "has_evaluation": (skill_dir / "EVALUATION.md").is_file(),
            "has_benchmark": benchmark.is_file(),
            "question_count": question_count,
        })
    return rows


def _find_skill_dir(skill_name: str) -> Path:
    base = (PROJECT_ROOT / "compiled_skill").resolve()
    skill_dir = (base / _slug(skill_name)).resolve()
    if skill_dir.parent != base or not (skill_dir / "SKILL.md").is_file():
        raise FileNotFoundError(f"未找到已编译 Skill: {skill_name}")
    return skill_dir


def load_benchmark_questions(skill_dir: Path) -> list[dict[str, Any]]:
    path = skill_dir / "benchmark.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 benchmark.jsonl: {skill_dir.name}")
    questions = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"benchmark.jsonl 第 {line_no} 行不是有效 JSON") from exc
        if isinstance(item, dict) and str(item.get("input") or "").strip():
            questions.append(item)
    if len(questions) < 2:
        raise ValueError("LIFT Suite 至少需要 2 道题，才能划分 warmup 与 holdout")
    return questions


def _numbered(lines: list[str]) -> str:
    cleaned = []
    seen = set()
    for line in lines:
        text = re.sub(r"^\s*\d+[.)、]\s*", "", str(line or "").strip())
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return "\n".join(f"{idx}. {line}" for idx, line in enumerate(cleaned, start=1))


def _content_requirements(question: dict[str, Any]) -> str:
    gold = question.get("gold") if isinstance(question.get("gold"), dict) else {}
    expected = gold.get("expected_label") if isinstance(gold.get("expected_label"), dict) else {}
    lines = [f"输出中的「{key}」应为「{value}」" for key, value in expected.items()]
    lines.extend(str(item) for item in (gold.get("must_hit") or []) if str(item).strip())
    lines.extend(f"不得出现或执行：{item}" for item in (gold.get("must_avoid") or []) if str(item).strip())
    if not lines:
        lines.append("给出与业务情境一致、可执行且不编造缺失事实的处理结果")
    return _numbered(lines)


def _trajectory_requirements(question: dict[str, Any]) -> str:
    sim = question.get("customer_sim") if isinstance(question.get("customer_sim"), dict) else {}
    hidden = [str(item).strip() for item in (sim.get("hidden_facts") or []) if str(item).strip()]
    lines = []
    if hidden:
        lines.append("信息不足时主动核验关键事实，不得把未披露信息当作已知事实")
    reveal_rules = str(sim.get("reveal_rules") or "").strip()
    if reveal_rules:
        lines.append(f"按照情境的信息披露边界推进核验：{reveal_rules}")
    lines.append(
        "只使用完成任务所必需的工具和步骤，并遵守题面中的权限、审批与安全边界"
    )
    return _numbered(lines)


def _task_from_question(
    question: dict[str, Any],
    task_name: str,
    extra_skills_dir: str,
) -> dict[str, Any]:
    return {
        "name": task_name,
        "query": str(question.get("input") or "").strip(),
        "requirements": {
            "default_skills": [],
            "extra_skills_dir": extra_skills_dir,
            "material_dir": "",
        },
        "expected_result": {
            "content_reqs": _content_requirements(question),
            "trajectory_reqs": _trajectory_requirements(question),
        },
    }


def build_suite_from_questions(
    skill_name: str,
    questions: list[dict[str, Any]],
    *,
    suite_name: str | None = None,
    category: str | None = None,
    warmup_ratio: float = 2 / 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(questions) < 2:
        raise ValueError("LIFT Suite 至少需要 2 道题")
    ratio = max(0.5, min(0.8, float(warmup_ratio)))
    ordered = sorted(
        questions,
        key=lambda q: (
            _DIFFICULTY_ORDER.get(str(q.get("difficulty") or "medium").lower(), 1),
            str(q.get("id") or ""),
            str(q.get("input") or ""),
        ),
    )
    warmup_count = max(1, min(len(ordered) - 1, round(len(ordered) * ratio)))
    warmup_questions = ordered[:warmup_count]
    holdout_questions = ordered[warmup_count:]

    suite_slug = _slug(suite_name or skill_name).lower()
    extra_skills_dir = f"assets/benchmark_mds/teamEvolver/{suite_slug}/skills"
    suite = {
        "name": str(suite_name or skill_name).strip() or suite_slug,
        "category": str(category or skill_name).strip() or suite_slug,
        "warmup_tasks": [
            _task_from_question(q, f"W{idx}", extra_skills_dir)
            for idx, q in enumerate(warmup_questions, start=1)
        ],
        "holdout_tasks": [
            _task_from_question(q, f"H{idx}", extra_skills_dir)
            for idx, q in enumerate(holdout_questions, start=1)
        ],
    }

    warmup_dims = {
        str(dim) for q in warmup_questions for dim in (q.get("target_dimensions") or []) if str(dim).strip()
    }
    holdout_dims = {
        str(dim) for q in holdout_questions for dim in (q.get("target_dimensions") or []) if str(dim).strip()
    }
    overlap = len(warmup_dims & holdout_dims) / len(holdout_dims) * 100 if holdout_dims else 100.0
    source_map = {
        **{f"W{i}": str(q.get("id") or "") for i, q in enumerate(warmup_questions, start=1)},
        **{f"H{i}": str(q.get("id") or "") for i, q in enumerate(holdout_questions, start=1)},
    }
    metrics = {
        "warmup_count": len(warmup_questions),
        "holdout_count": len(holdout_questions),
        "warmup_ratio": round(len(warmup_questions) / len(ordered), 4),
        "dimension_overlap_pct": round(overlap, 2),
        "warmup_dimensions": sorted(warmup_dims),
        "holdout_dimensions": sorted(holdout_dims),
        "source_question_map": source_map,
    }
    return suite, metrics


def _normalize_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        task = {}
    requirements = task.get("requirements") if isinstance(task.get("requirements"), dict) else {}
    expected = task.get("expected_result") if isinstance(task.get("expected_result"), dict) else {}
    default_skills = requirements.get("default_skills")
    if not isinstance(default_skills, list):
        default_skills = []
    return {
        "name": str(task.get("name") or "").strip(),
        "query": str(task.get("query") or "").strip(),
        "requirements": {
            "default_skills": [str(item).strip() for item in default_skills if str(item).strip()],
            "extra_skills_dir": str(requirements.get("extra_skills_dir") or "").strip(),
            "material_dir": str(requirements.get("material_dir") or "").strip(),
        },
        "expected_result": {
            "content_reqs": str(expected.get("content_reqs") or "").strip(),
            "trajectory_reqs": str(expected.get("trajectory_reqs") or "").strip(),
        },
    }


def normalize_suite(suite: Any) -> dict[str, Any]:
    if not isinstance(suite, dict):
        suite = {}
    return {
        "name": str(suite.get("name") or "").strip(),
        "category": str(suite.get("category") or "").strip(),
        "warmup_tasks": [_normalize_task(task) for task in (suite.get("warmup_tasks") or [])],
        "holdout_tasks": [_normalize_task(task) for task in (suite.get("holdout_tasks") or [])],
    }


def _align_extra_skills_dir(suite: dict[str, Any]) -> None:
    extra_skills_dir = f"assets/benchmark_mds/teamEvolver/{_slug(suite['name']).lower()}/skills"
    for task in suite["warmup_tasks"] + suite["holdout_tasks"]:
        task["requirements"]["extra_skills_dir"] = extra_skills_dir


def validate_suite(suite: Any) -> dict[str, Any]:
    suite = normalize_suite(suite)
    errors: list[str] = []
    warnings: list[str] = []
    if not suite["name"]:
        errors.append("Suite name 不能为空")
    if not suite["category"]:
        errors.append("Suite category 不能为空")
    if not suite["warmup_tasks"]:
        errors.append("至少需要 1 道 warmup task")
    if not suite["holdout_tasks"]:
        errors.append("至少需要 1 道 holdout task")

    names: set[str] = set()
    for split_name, tasks in (("warmup", suite["warmup_tasks"]), ("holdout", suite["holdout_tasks"])):
        for idx, task in enumerate(tasks, start=1):
            prefix = f"{split_name}[{idx}]"
            if not task["name"]:
                errors.append(f"{prefix}.name 不能为空")
            elif task["name"] in names:
                errors.append(f"task name 重复: {task['name']}")
            names.add(task["name"])
            if not task["query"]:
                errors.append(f"{prefix}.query 不能为空")
            expected = task["expected_result"]
            if not expected["content_reqs"]:
                errors.append(f"{prefix}.content_reqs 不能为空")
            elif not re.search(r"(?m)^\s*\d+[.)、]", expected["content_reqs"]):
                warnings.append(f"{prefix}.content_reqs 建议拆成可独立验证的编号要求")
            if not expected["trajectory_reqs"]:
                warnings.append(f"{prefix}.trajectory_reqs 为空，LIFT 无法评估执行路径")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "warmup_count": len(suite["warmup_tasks"]),
        "holdout_count": len(suite["holdout_tasks"]),
        "task_count": len(suite["warmup_tasks"]) + len(suite["holdout_tasks"]),
    }


def _copy_skill_bundle(skill_dir: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("SKILL.md", "EVALUATION.md"):
        source = skill_dir / filename
        if source.is_file():
            shutil.copy2(source, destination / filename)
    for dirname in _BUNDLE_DIRS:
        source = skill_dir / dirname
        if source.is_dir():
            shutil.copytree(source, destination / dirname, dirs_exist_ok=True)


def _write_source_layout(draft_dir: Path, suite: dict[str, Any], skill_dir: Path) -> None:
    source_root = draft_dir / "source"
    if source_root.exists():
        shutil.rmtree(source_root)
    suite_slug = _slug(suite["name"])
    scene_dir = source_root / suite_slug
    for split_dir_name, key in (("train", "warmup_tasks"), ("test", "holdout_tasks")):
        for idx, task in enumerate(suite[key], start=1):
            task_slug = _slug(task["name"], f"task-{idx}").lower()
            task_dir = scene_dir / split_dir_name / f"q{idx}_{task_slug}"
            task_dir.mkdir(parents=True, exist_ok=True)
            md_path = task_dir / f"q{idx}_{task_slug}.md"
            md_path.write_text(
                "### query\n\n"
                f"{task['query']}\n\n"
                "### 要求\n\n"
                f"{task['expected_result']['content_reqs']}\n\n"
                "### 轨迹要求\n\n"
                f"{task['expected_result']['trajectory_reqs']}\n",
                encoding="utf-8",
            )
    _copy_skill_bundle(skill_dir, scene_dir / "skills" / _slug(skill_dir.name))


def create_draft(
    skill_name: str,
    *,
    suite_name: str | None = None,
    category: str | None = None,
    warmup_ratio: float = 2 / 3,
    questions: list[dict[str, Any]] | None = None,
    origin: str = "manual",
) -> dict[str, Any]:
    skill_dir = _find_skill_dir(skill_name)
    questions = questions or load_benchmark_questions(skill_dir)
    suite, metrics = build_suite_from_questions(
        skill_name,
        questions,
        suite_name=suite_name,
        category=category,
        warmup_ratio=warmup_ratio,
    )
    validation = validate_suite(suite)
    draft_id = f"{_slug(suite['name']).lower()}-{_timestamp()}"
    draft_dir = _safe_child(DRAFTS_DIR, draft_id)
    draft_dir.mkdir(parents=True, exist_ok=False)
    _write_json(draft_dir / "suite.json", suite)
    _write_source_layout(draft_dir, suite, skill_dir)
    manifest = {
        "id": draft_id,
        "schema": LIFT_SCHEMA,
        "state": "draft",
        "origin": origin,
        "source_skill": skill_dir.name,
        "suite_slug": _slug(suite["name"]).lower(),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "reviewed_at": None,
        "reviewed_by": None,
        "review_note": "",
        "published_at": None,
        "published_paths": {},
        "metrics": metrics,
        "validation": validation,
    }
    _write_json(draft_dir / "manifest.json", manifest)
    return {"manifest": manifest, "suite": suite}


def _draft_dir(draft_id: str) -> Path:
    path = _safe_child(DRAFTS_DIR, draft_id)
    if not path.is_dir():
        raise FileNotFoundError(f"未找到 LIFT 草稿: {draft_id}")
    return path


def get_draft(draft_id: str) -> dict[str, Any]:
    path = _draft_dir(draft_id)
    return {"manifest": _read_json(path / "manifest.json"), "suite": _read_json(path / "suite.json")}


def list_drafts() -> list[dict[str, Any]]:
    rows = []
    if not DRAFTS_DIR.is_dir():
        return rows
    for path in sorted(DRAFTS_DIR.iterdir(), reverse=True):
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            rows.append(_read_json(manifest_path))
        except Exception:
            continue
    return rows


def save_draft(draft_id: str, suite: Any) -> dict[str, Any]:
    path = _draft_dir(draft_id)
    manifest = _read_json(path / "manifest.json")
    normalized = normalize_suite(suite)
    _align_extra_skills_dir(normalized)
    validation = validate_suite(normalized)
    skill_dir = _find_skill_dir(str(manifest.get("source_skill") or ""))
    _write_json(path / "suite.json", normalized)
    _write_source_layout(path, normalized, skill_dir)
    manifest.update({
        "state": "draft",
        "suite_slug": _slug(normalized["name"]).lower(),
        "updated_at": _utc_now(),
        "reviewed_at": None,
        "reviewed_by": None,
        "review_note": "",
        "validation": validation,
    })
    _write_json(path / "manifest.json", manifest)
    return {"manifest": manifest, "suite": normalized}


def approve_draft(draft_id: str, reviewer: str, note: str = "") -> dict[str, Any]:
    path = _draft_dir(draft_id)
    manifest = _read_json(path / "manifest.json")
    suite = _read_json(path / "suite.json")
    validation = validate_suite(suite)
    if not validation["valid"]:
        raise ValueError("数据集校验未通过，不能批准：" + "；".join(validation["errors"]))
    manifest.update({
        "state": "approved",
        "updated_at": _utc_now(),
        "reviewed_at": _utc_now(),
        "reviewed_by": str(reviewer or "human-reviewer").strip()[:120],
        "review_note": str(note or "").strip()[:2000],
        "validation": validation,
    })
    _write_json(path / "manifest.json", manifest)
    return {"manifest": manifest, "suite": suite}


def _backup_existing(root: Path, scene_target: Path, json_target: Path) -> Path | None:
    if not scene_target.exists() and not json_target.exists():
        return None
    backup_root = root / "assets" / "teamEvolver_import_history" / _timestamp()
    if scene_target.exists():
        destination = backup_root / "benchmark_mds" / scene_target.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(scene_target), str(destination))
    if json_target.exists():
        destination = backup_root / "benchmarks" / json_target.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(json_target), str(destination))
    return backup_root


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _restore_backup(backup_root: Path | None, scene_target: Path, json_target: Path) -> None:
    """Remove a partial publish and put the previous LIFT dataset back."""
    _remove_path(scene_target)
    _remove_path(json_target)
    if backup_root is None:
        return
    backup_scene = backup_root / "benchmark_mds" / scene_target.name
    backup_json = backup_root / "benchmarks" / json_target.name
    if backup_scene.exists():
        scene_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_scene), str(scene_target))
    if backup_json.exists():
        json_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_json), str(json_target))
    try:
        backup_root.rmdir()
    except OSError:
        pass


def publish_draft(draft_id: str) -> dict[str, Any]:
    path = _draft_dir(draft_id)
    manifest = _read_json(path / "manifest.json")
    suite = normalize_suite(_read_json(path / "suite.json"))
    if manifest.get("state") != "approved":
        raise ValueError("只有人工审核通过的草稿才能发布到 LIFT")
    validation = validate_suite(suite)
    if not validation["valid"]:
        raise ValueError("发布前校验失败：" + "；".join(validation["errors"]))

    status = lift_status()
    if not status["checkout_ready"]:
        raise FileNotFoundError(
            "LIFT checkout 未就绪。请设置 TEAMEVOLVER_LIFT_ROOT，或运行 scripts/setup_lift.sh"
        )
    root = Path(status["root"])
    suite_slug = _slug(suite["name"]).lower()
    scene_target = root / "assets" / "benchmark_mds" / "teamEvolver" / suite_slug
    json_target = root / "assets" / "benchmarks" / "teamEvolver" / f"{suite_slug}.json"
    source_scene = path / "source" / _slug(suite["name"])
    if not source_scene.is_dir():
        raise FileNotFoundError("草稿缺少 LIFT Markdown 源目录")

    snapshot = PUBLISHED_DIR / f"{draft_id}-{_timestamp()}"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, snapshot)
    backup_root: Path | None = None
    try:
        backup_root = _backup_existing(root, scene_target, json_target)
        scene_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_scene, scene_target)
        _write_json(json_target, suite)

        manifest.update({
            "state": "published",
            "updated_at": _utc_now(),
            "published_at": _utc_now(),
            "published_paths": {
                "markdown_scene": str(scene_target),
                "suite_json": str(json_target),
                "backup": str(backup_root) if backup_root else "",
                "snapshot": str(snapshot),
            },
            "validation": validation,
        })
        _write_json(snapshot / "manifest.json", manifest)
        _write_json(path / "manifest.json", manifest)
    except Exception:
        _restore_backup(backup_root, scene_target, json_target)
        _remove_path(snapshot)
        raise
    return {"manifest": manifest, "suite": suite}


def build_lift_command(options: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    status = lift_status()
    if not status["checkout_ready"]:
        raise FileNotFoundError("LIFT checkout 未就绪")
    if not status["python_ready"]:
        version = status.get("python_version") or "未检测到"
        raise RuntimeError(f"LIFT 需要 Python 3.12+；当前版本：{version}")
    runtime = str(options.get("runtime") or "hermes").strip()
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"不支持的 LIFT runtime: {runtime}")
    suite_file = _slug(str(options.get("suite") or ""), "")
    if not suite_file:
        raise ValueError("suite 不能为空")
    if not suite_file.endswith(".json"):
        suite_file += ".json"
    repeat = max(1, min(5, int(options.get("repeat") or 1)))
    max_parallel = max(1, min(10, int(options.get("max_parallel_suites") or 1)))
    run_suffix = _slug(str(options.get("run_id") or f"teamEvolver-{_timestamp()}"))
    root = Path(status["root"])
    suite_path = root / "assets" / "benchmarks" / "teamEvolver" / suite_file
    if not suite_path.is_file():
        raise FileNotFoundError(f"LIFT 中尚未发布该 Suite: {suite_file}")
    cmd = [
        status["python"], "-m", "src.cli.lift_main",
        "-r", runtime,
        "--benchmark_dir", str(root / "assets" / "benchmarks"),
        "--suite", suite_file,
        "--run-id", run_suffix,
        "--repeat", str(repeat),
        "--max-parallel-suites", str(max_parallel),
    ]
    if runtime.startswith("hermes"):
        cmd.extend(["--warmup-container-policy", "serial_single"])
    metadata = {
        "id": run_suffix,
        "runtime": runtime,
        "suite": suite_file,
        "repeat": repeat,
        "max_parallel_suites": max_parallel,
        "root": str(root),
        "command": cmd,
        "result_dir": str(root / "results" / f"lift-runid-{run_suffix}"),
    }
    return cmd, metadata


class LiftRunManager:
    """Runs one external LIFT CLI process and streams structured events."""

    def __init__(self, emit: Callable[..., None]):
        self.emit = emit
        self.state = "idle"
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.current: dict[str, Any] | None = None
        self._stop_requested = False
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "current": self.current}

    def start(self, options: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        with self._lock:
            if self.state in {"running", "stopping"}:
                return False, "已有 LIFT 任务在运行", self.current
            cmd, metadata = build_lift_command(options)
            metadata.update({"state": "running", "started_at": _utc_now(), "finished_at": None, "exit_code": None})
            self.current = metadata
            self.state = "running"
            self._stop_requested = False
            _write_json(RUNS_DIR / f"{metadata['id']}.json", metadata)
            self.thread = threading.Thread(target=self._run, args=(cmd, metadata), daemon=True)
            self.thread.start()
            return True, "started", metadata

    def _run(self, cmd: list[str], metadata: dict[str, Any]) -> None:
        self.emit("lift_status", state="running", run=metadata)
        self.emit("lift_log", msg="LIFT 运行开始")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                cmd,
                cwd=metadata["root"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=os.name != "nt",
            )
            with self._lock:
                self.process = process
                stop_requested = self._stop_requested
            if stop_requested:
                self._terminate_process(process)
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip()
                if clean:
                    self.emit("lift_log", msg=clean)
            exit_code = process.wait()
            with self._lock:
                was_stopped = self._stop_requested
            final_state = "stopped" if was_stopped else ("done" if exit_code == 0 else "error")
        except Exception as exc:
            exit_code = -1
            final_state = "error"
            self.emit("lift_log", msg=f"LIFT 启动失败: {exc}", level="error")
        finally:
            metadata.update({"state": final_state, "finished_at": _utc_now(), "exit_code": exit_code})
            _write_json(RUNS_DIR / f"{metadata['id']}.json", metadata)
            with self._lock:
                self.state = final_state
                self.current = metadata
                self.process = None
                self._stop_requested = False
            self.emit("lift_status", state=final_state, run=metadata)
            self.emit("lift_done", state=final_state, run=metadata)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            process = self.process
            if self.state != "running":
                return False, "当前没有正在运行的 LIFT 任务"
            self.state = "stopping"
            self._stop_requested = True
        if process is not None:
            self._terminate_process(process)
        self.emit("lift_status", state="stopping", run=self.current)
        return True, "stopping"
