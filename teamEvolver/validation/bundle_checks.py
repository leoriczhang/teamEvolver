"""Deterministic validation gates for candidate bundle changes."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..skills.bundle import candidate_skill_bundle, write_skill_bundle


def validate_candidate_bundle(
    skill: Mapping[str, object],
    *,
    enabled: bool = True,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Validate changed Python and Shell files without executing them."""
    changes = [
        dict(item)
        for item in (skill.get("file_changes") or [])
        if isinstance(item, Mapping)
    ]
    changed_paths = [
        str(item.get("path") or "")
        for item in changes
        if str(item.get("operation") or "").lower() == "upsert"
    ]
    if not enabled:
        return {
            "passed": True,
            "enabled": False,
            "checks": [],
            "errors": [],
            "changed_files": changed_paths,
        }

    bundle = candidate_skill_bundle(skill)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="teamevolver_bundle_check_") as tmp:
        root = Path(tmp)
        write_skill_bundle(root, bundle, clean=False)
        for rel_path in changed_paths:
            path = root / rel_path
            suffix = path.suffix.lower()
            if suffix == ".py":
                command = [sys.executable, "-m", "py_compile", str(path)]
                checker = "py_compile"
            elif suffix == ".sh":
                bash = shutil.which("bash")
                if not bash:
                    message = f"bash is unavailable for static check: {rel_path}"
                    checks.append(
                        {
                            "path": rel_path,
                            "checker": "bash_n",
                            "passed": False,
                            "detail": message,
                        }
                    )
                    errors.append(message)
                    continue
                command = [bash, "-n", "--", str(path)]
                checker = "bash_n"
            else:
                checks.append(
                    {
                        "path": rel_path,
                        "checker": "utf8_contract",
                        "passed": True,
                        "detail": "No language-specific checker configured.",
                    }
                )
                continue
            try:
                result = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, float(timeout_seconds)),
                    check=False,
                )
                passed = result.returncode == 0
                detail = (
                    (result.stderr or result.stdout or "").strip()[-2000:]
                    if not passed
                    else ""
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                passed = False
                detail = f"{type(exc).__name__}: {exc}"
            checks.append(
                {
                    "path": rel_path,
                    "checker": checker,
                    "passed": passed,
                    "detail": detail,
                }
            )
            if not passed:
                errors.append(f"{rel_path}: {detail or checker + ' failed'}")
    return {
        "passed": not errors,
        "enabled": True,
        "checks": checks,
        "errors": errors,
        "changed_files": changed_paths,
    }
