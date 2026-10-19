"""Platform dependency direction, unique routes and retired implementation boundaries."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

# Business (domain) top-level packages.
BUSINESS_PACKAGES = {
    "session_ingestion",
    "team_skills",
    "team_replay",
    "team_memory",
    "team_miner",
}

# Platform infrastructure modules that must never import business packages
# are expected to stay infrastructure-only.
PLATFORM_INFRA_PATHS = [
    "teamEvolver/storage",
    "teamEvolver/observability",
    "teamEvolver/database",
    "teamEvolver/openviking",
    "teamEvolver/security",
]

# Old implementations that were deleted in the first cleanup round and must not be
# revived .
FORBIDDEN_REVIVAL_FILES = [
    "teamEvolver/integrations/agent_protocol.py",
    "teamEvolver/integrations/agent_registry.py",
    "teamEvolver/integrations/skill_sync_adapters.py",
]

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
SKIP_RECEIVERS = {"self", "cls", "logger", "log"}


def _iter_py_files(base: Path):
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_deleted_old_implementations_are_not_revived():
    revived = [f for f in FORBIDDEN_REVIVAL_FILES if (REPO_ROOT / f).exists()]
    assert not revived, f"Deleted old agent-registry implementations were revived: {revived}"


@pytest.mark.parametrize("infra", PLATFORM_INFRA_PATHS)
def test_platform_infra_does_not_import_business_modules(infra: str):
    base = REPO_ROOT / infra
    assert base.exists(), f"required platform infrastructure missing: {infra}"
    offenders: list[str] = []
    for path in _iter_py_files(base):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in BUSINESS_PACKAGES:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}")
    assert not offenders, "Platform infra imports business modules (reverse dependency):\n" + "\n".join(offenders)


def _routes_in_file(path: Path):
    routes = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return routes
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            method = dec.func.attr.lower()
            if method not in HTTP_METHODS:
                continue
            receiver = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
            if receiver in SKIP_RECEIVERS:
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                routes.append((method.upper(), dec.args[0].value))
    return routes


def test_no_duplicate_route_within_a_single_router_file():
    duplicates: dict[str, list[str]] = {}
    for pkg in ["teamEvolver", *sorted(BUSINESS_PACKAGES)]:
        base = REPO_ROOT / pkg
        if not base.exists():
            continue
        for path in _iter_py_files(base):
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            counts = Counter(_routes_in_file(path))
            dups = [f"{m} {p}" for (m, p), n in counts.items() if n > 1]
            if dups:
                duplicates[str(path.relative_to(REPO_ROOT))] = dups
    assert not duplicates, f"Duplicate path+method within a router file: {duplicates}"


def test_composed_routes_cover_every_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("endpoint_index", REPO_ROOT / "scripts/docs_endpoint_index.py")
    index = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(index)
    routes = index.composed_routes()
    assert {module for _, _, module in routes} == set(index.MODULES)
    assert ("GET", "/api/auth/status", "teamEvolver/admin") in routes
    assert len(routes) == len({(method, path) for method, path, _ in routes})
