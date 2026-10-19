"""The former standalone evolution app cannot be reconstructed or dispatched."""

import ast

from tests.support.paths import REPO_ROOT

ROOT = REPO_ROOT


def test_old_evolution_http_and_lifecycle_are_removed():
    path = ROOT / "team_skills/evolution/runtime/orchestrator.py"
    assert not path.exists()
    assert not (ROOT / "teamEvolver/proxy").exists()


def test_new_evolution_does_not_own_http_lifecycle():
    for filename in ("cycle.py", "candidate_pipeline.py", "session_processing.py", "validation.py"):
        tree = ast.parse((ROOT / "team_skills/evolution" / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("fastapi", "teamEvolver.proxy"))
