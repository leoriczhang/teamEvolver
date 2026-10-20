"""Final deletion, dependency boundaries and composed-route gates."""

import ast
import re
from types import SimpleNamespace

from teamEvolver.app import create_app
from teamEvolver.config import TeamEvolverConfig
from tests.support.paths import REPO_ROOT

ROOT = REPO_ROOT
PACKAGES = (
    "teamEvolver",
    "session_ingestion",
    "team_skills",
    "team_replay",
    "team_memory",
    "team_miner",
)
RETIRED = (
    "teamEvolver/config_store",
    "teamEvolver/launcher.py",
    "teamEvolver/proxy",
    "teamEvolver/session_store.py",
    "teamEvolver/true_replay.py",
    "teamEvolver/replay_adapter.py",
    "teamEvolver/local_state.py",
    "teamEvolver/integrations/context_workspace.py",
    "teamEvolver/integrations/delivery_spool.py",
    "teamEvolver/integrations/agent_principal.py",
    "teamEvolver/storage/pg_pool.py",
    "session_ingestion/push/routes.py",
    "session_ingestion/pull/routes.py",
    "team_skills/evolution/runtime/orchestrator.py",
    "team_skills/evolution/store",
    "team_skills/candidates/worker.py",
    "team_skills/library/experience_sync.py",
    "team_skills/delivery",
    "team_skills/api/delivery.py",
    "team_skills/library/mirror.py",
    "team_replay/adapter_runtime.py",
    "team_replay/customer_adapters",
    "team_replay/lab",
    "team_memory/service.py",
    "team_memory/compile_client.py",
    "team_memory/routes.py",
    "team_memory/memory_routes.py",
    "team_memory/debug_routes.py",
    "team_memory/frontend",
)


def sources():
    for pkg in PACKAGES:
        for path in (ROOT / pkg).rglob("*.py"):
            if "tests" not in path.parts and "__pycache__" not in path.parts:
                yield path


def test_retired_paths_absent_and_no_imports():
    assert not [p for p in RETIRED if (ROOT / p).exists()]
    modules = [p.removesuffix(".py").replace("/", ".") for p in RETIRED]
    errors = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            )
            for name in names:
                if any(name == old or name.startswith(old + ".") for old in modules):
                    errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert not errors, errors


def test_fastapi_only_in_http_boundaries_and_no_lazy_package_exports():
    errors = []
    for path in sources():
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            )
            if any(name.startswith("fastapi") for name in names) and not (
                "/api/" in relative or relative in {"teamEvolver/app.py", "teamEvolver/runtime/http_runtime.py"}
            ):
                errors.append(relative)
            if path.name == "__init__.py" and isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                errors.append(relative + ": lazy exports")
    assert not errors, errors


def test_production_line_ceiling_including_frontend():
    files = [
        *sources(),
        *(ROOT / "web-ui/src").rglob("*.ts"),
        *(ROOT / "web-ui/src").rglob("*.tsx"),
        *(ROOT / "team_miner/knowledge_miner/frontend").rglob("*.tsx"),
        *(ROOT / "team_miner/skill_miner/frontend").rglob("*.tsx"),
    ]
    assert not [
        str(p.relative_to(ROOT)) for p in files if ".test." not in p.name and len(p.read_text().splitlines()) > 1000
    ]


def test_composed_api_contains_current_frontend_routes_once():
    extras = {
        "config": TeamEvolverConfig(),
        "config_repo": object(),
        "session_payloads": object(),
        "console_sessions": object(),
    }
    services = SimpleNamespace(
        extras=extras,
        tx=SimpleNamespace(pool=SimpleNamespace(schema="te")),
        tasks=object(),
        leases=object(),
        role="api",
    )
    app = create_app(services=services)
    paths = app.openapi()["paths"]
    required = {
        "/api/datasource",
        "/api/datasource/pull",
        "/api/datasource/code",
        "/api/datasource/schedule",
        "/api/sessions/page",
        "/api/sessions/{session_id}/content",
        "/api/skill-evolution/prompts",
        "/api/skill-evolution/settings",
        "/api/model-settings",
        "/api/replay-lab/skills/{name}/save",
        "/api/replay-lab/datasets/{dataset_id}/clone",
        "/api/replay-lab/runs",
        "/api/datasets",
        "/api/platform-assets/content",
        "/api/skills/transfer/skills",
    }
    assert not required - set(paths)
    from collections import Counter

    def pairs(routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested:
                yield from pairs(nested.routes)
            elif getattr(route, "path", None):
                for method in getattr(route, "methods", ()):
                    yield method, route.path

    duplicates = [pair for pair, count in Counter(pairs(app.routes)).items() if count > 1]
    assert not duplicates


def test_frontend_features_and_single_miner_build():
    for domain in (
        "admin",
        "tenants",
        "sessions",
        "skills",
        "replay",
        "memory",
        "operations",
        "platform-docs",
    ):
        assert (ROOT / "web-ui/src/features" / domain).is_dir()
    assert not (ROOT / "web-ui/src/views").exists()
    assert (ROOT / "team_miner/knowledge_miner/frontend").is_dir()
    assert (ROOT / "team_miner/skill_miner/frontend").is_dir()
    assert not list((ROOT / "team_miner").rglob("package.json"))
    aliases = (ROOT / "web-ui/vite.config.ts").read_text()
    assert "@skill-miner" in aliases
    assert "@knowledge-miner" in aliases


def test_old_workbench_wire_names_removed_from_frontend():
    files = [
        *(ROOT / "web-ui/src/features/skills/workbench").glob("*.tsx"),
        ROOT / "web-ui/src/features/replay/api/client.ts",
    ]
    for path in files:
        text = path.read_text()
        assert "SkillLab" not in text
        if path.name != "client.ts":
            assert not re.search(r"skill_name\s*:", text), path


def test_bootstrap_rejects_retired_sections_and_worker_config_keys(tmp_path):
    import pytest

    from teamEvolver.bootstrap import load_bootstrap

    path = tmp_path / "config.yaml"
    for value in (
        "evolve:\n  enabled: true\n",
        "evolve_use_session_judge: false\n",
        "sharing_skill_mirror_enabled: true\n",
    ):
        path.write_text(value)
        with pytest.raises(ValueError, match="unsupported bootstrap fields"):
            load_bootstrap(path=path, environ={})


def test_shipped_bootstrap_templates_use_current_fields():
    from teamEvolver.bootstrap import load_bootstrap

    for path in (ROOT / "config").glob("*.yaml"):
        load_bootstrap(path=path, environ={})
