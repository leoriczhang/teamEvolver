"""Regression checks for the canonical Team Memory package layout."""

from dataclasses import fields, replace
from importlib import import_module
from pathlib import Path

import pytest

from team_memory.config import MEMORY_DEFAULTS, TeamMemoryConfig, memory_config_values


def test_team_memory_has_one_package_owner():
    repo_root = Path(__file__).resolve().parents[2]
    removed_paths = [
        "teamEvolver/aggregation",
        "teamEvolver/dreamcycle",
        "teamEvolver/team_memory",
        "teamEvolver/integrations/dreamcycle.py",
        "teamEvolver/integrations/dreamcycle_runtime.py",
        "teamEvolver/integrations/native_dreamcycle.py",
        "teamEvolver/proxy/aggregation_routes.py",
        "teamEvolver/proxy/memory_debug.py",
        "team_memory/_compat.py",
    ]

    assert not [path for path in removed_paths if (repo_root / path).exists()]
    owner_root = repo_root / "team_memory"
    for module_name in (
        "team_memory",
        "team_memory.routes",
        "team_memory.debug_routes",
        "team_memory.maintenance.runtime",
    ):
        module = import_module(module_name)
        assert Path(module.__file__).resolve().is_relative_to(owner_root)


def test_config_fields_are_owned_by_memory_package():
    from teamEvolver.config import TeamEvolverConfig

    inherited = {item.name for item in fields(TeamMemoryConfig)}
    actual = {item.name for item in fields(TeamEvolverConfig) if item.name.startswith(("aggregation_", "dreamcycle_"))}
    assert inherited == actual
    config = replace(TeamEvolverConfig(), aggregation_maintenance_skill_uri="viking://agent/skills/custom")
    assert config.aggregation_maintenance_skill_uri.endswith("/custom")
    assert TeamEvolverConfig().aggregation_kinds is not TeamEvolverConfig().aggregation_kinds


def test_default_normalization_preserves_existing_contract():
    normalized = memory_config_values(MEMORY_DEFAULTS, lambda values: [str(value) for value in (values or [])])
    config = TeamMemoryConfig()
    assert set(normalized) == {item.name for item in fields(config)}
    for name, value in normalized.items():
        assert value == getattr(config, name), name


def test_packaging_and_build_inputs_include_sibling_package():
    root = Path(__file__).resolve().parents[2]
    tomllib = pytest.importorskip("tomllib")

    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert "team_memory*" in project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "team_replay*" in project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "team_memory/tests" in project["tool"]["pytest"]["ini_options"]["testpaths"]
    assert "team_replay/tests" in project["tool"]["pytest"]["ini_options"]["testpaths"]
    assert project["project"]["scripts"]["teamEvolver-dreamcycle"] == "team_memory.__main__:main"
    assert "COPY team_memory/" in (root / "Dockerfile").read_text()
    assert "COPY team_replay/" in (root / "Dockerfile").read_text()
    assert "COPY team_memory " in (root / "docker/Dockerfile.customer").read_text()
    assert "COPY team_replay " in (root / "docker/Dockerfile.customer").read_text()
    assert 'copy_dir  "team_memory"' in (root / "cicd_scripts/build.sh").read_text()
    assert 'copy_dir  "team_replay"' in (root / "cicd_scripts/build.sh").read_text()


def test_team_replay_is_the_only_replay_directory():
    root = Path(__file__).resolve().parents[2]

    replay_dirs = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
        and path.name.lower() in {"replay", "team_replay"}
        and ".git" not in path.parts
        and "build" not in path.parts
        and "runtime" not in path.parts
        and "__pycache__" not in path.parts
    )

    assert replay_dirs == ["team_replay"]
    assert import_module("teamEvolver.replay.engine") is import_module(
        "team_replay.engine"
    )
    assert import_module("team_memory.replay.memory_changes") is import_module(
        "team_memory.memory_changes"
    )


@pytest.mark.parametrize(
    "module",
    ["team_memory.routes", "team_memory.debug_routes", "team_memory.maintenance.runtime"],
)
def test_cold_import_does_not_require_proxy_to_be_loaded_first(module):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-c", f"import {module}"],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
