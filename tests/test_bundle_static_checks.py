from __future__ import annotations

import shutil

import pytest

from teamEvolver.skills.bundle import attach_bundle_payload
from teamEvolver.validation.bundle_checks import validate_candidate_bundle


def _candidate(path: str, content: str) -> dict[str, object]:
    skill = {
        "name": "demo",
        "description": "Demo",
        "category": "general",
        "content": "# Demo",
    }
    return attach_bundle_payload(
        skill,
        {
            "SKILL.md": b"# stale",
            path: content.encode("utf-8"),
        },
        file_changes=[
            {
                "path": path,
                "operation": "upsert",
                "reason": "test",
            }
        ],
    )


@pytest.mark.parametrize(
    "path,content",
    [
        ("scripts/run.py", "print('ok')\n"),
        ("scripts/run.sh", "set -e\necho ok\n"),
    ],
)
def test_static_checks_accept_valid_scripts(path: str, content: str) -> None:
    if path.endswith(".sh") and not shutil.which("bash"):
        pytest.skip("bash unavailable")
    result = validate_candidate_bundle(_candidate(path, content))

    assert result["passed"] is True
    assert result["checks"][0]["passed"] is True


@pytest.mark.parametrize(
    "path,content",
    [
        ("scripts/run.py", "def broken(:\n"),
        ("scripts/run.sh", "if true; then\n"),
    ],
)
def test_static_checks_reject_invalid_scripts(path: str, content: str) -> None:
    if path.endswith(".sh") and not shutil.which("bash"):
        pytest.skip("bash unavailable")
    result = validate_candidate_bundle(_candidate(path, content))

    assert result["passed"] is False
    assert result["errors"]


def test_static_checks_ignore_deleted_scripts() -> None:
    skill = {
        "name": "demo",
        "description": "Demo",
        "content": "# Demo",
        "file_changes": [
            {
                "path": "scripts/old.py",
                "operation": "delete",
                "reason": "obsolete",
            }
        ],
    }

    result = validate_candidate_bundle(skill)

    assert result["passed"] is True
    assert result["checks"] == []
