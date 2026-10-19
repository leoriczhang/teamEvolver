"""Importable module packages, concrete router factories and task registry contracts."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

BUSINESS_PACKAGES = {
    "session_ingestion",
    "team_skills",
    "team_replay",
    "team_memory",
    "team_miner",
}

# Required directories contain an importable ``__init__.py``.
PLATFORM_TARGET_PACKAGES = [
    "teamEvolver/runtime",
    "teamEvolver/admin/api",
    "teamEvolver/tenants/api",
    "teamEvolver/operations",
    "teamEvolver/operations/api",
    "teamEvolver/platform_docs",
    "teamEvolver/platform_docs/api",
    "teamEvolver/observability/api",
    "teamEvolver/context",
    "teamEvolver/context/api",
]

# Module API factories installed by the application root.
MODULE_API_PACKAGES = [
    "teamEvolver.admin.api",
    "teamEvolver.tenants.api",
    "teamEvolver.operations.api",
    "teamEvolver.platform_docs.api",
    "teamEvolver.observability.api",
    "teamEvolver.context.api",
    "session_ingestion.api",
    "team_skills.api",
    "team_replay.api",
    "team_memory.api",
    "team_miner.api",
]

# Runtime is platform infrastructure.
RUNTIME_INFRA_PATH = "teamEvolver/runtime"


def _iter_py_files(base: Path):
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


@pytest.mark.parametrize("pkg_path", PLATFORM_TARGET_PACKAGES)
def test_platform_target_package_exists(pkg_path: str):
    pkg = REPO_ROOT / pkg_path
    assert pkg.is_dir(), f"required module package missing: {pkg_path}"
    assert (pkg / "__init__.py").exists(), f"{pkg_path} is not an importable package (no __init__.py)"


@pytest.mark.parametrize("module", MODULE_API_PACKAGES)
def test_module_api_exposes_create_router(module: str):
    mod = importlib.import_module(module)
    factory = getattr(mod, "create_router", None)
    assert callable(factory), f"{module}.create_router missing or not callable "
    from fastapi import APIRouter

    from tests.support.composition import composition_services

    router = factory(composition_services())
    assert isinstance(router, APIRouter)
    assert router.routes, f"{module}.create_router returned an empty API surface"


def test_runtime_does_not_import_business_modules():
    base = REPO_ROOT / RUNTIME_INFRA_PATH
    assert base.exists(), "runtime package missing"
    offenders: list[str] = []
    for path in _iter_py_files(base):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in BUSINESS_PACKAGES:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}")
    assert not offenders, "runtime imports business modules (reverse dependency):\n" + "\n".join(offenders)


def test_worker_task_kinds_are_declared():
    from teamEvolver.runtime.tasks import TASK_KINDS

    expected = {
        "session.pull",
        "session.normalize",
        "session.split",
        "session.analyze",
        "session.archive",
        "skill.evolve",
        "skill.validate",
        "skill.transfer",
        "experience.sync",
        "replay.run",
        "dataset.run",
        "memory.aggregate",
        "memory.compile",
        "memory.replay",
        "miner.track_compile",
        "miner.rebuild_index",
    }
    assert set(TASK_KINDS) == expected, "declared task kinds changed"
    assert len(TASK_KINDS) == len(set(TASK_KINDS)), "duplicate task kind in TASK_KINDS"


def test_test_support_does_not_import_test_modules():
    offenders = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            )
            for name in names:
                if name.startswith("tests.") and any(part.startswith("test_") for part in name.split(".")):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {name}")
    assert not offenders, offenders


def test_test_collection_has_one_root_and_no_phase_files():
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert project["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert not list((REPO_ROOT / "tests").rglob("test_phase*.py"))
    assert not list((REPO_ROOT / "tests").rglob("test_scaffold*.py"))
    assert not any((REPO_ROOT / name / "tests").exists() for name in ("team_memory", "team_miner", "team_replay"))
