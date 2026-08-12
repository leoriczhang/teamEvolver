"""Isolation policy for SkillMiner's project-owned Hermes runtime.

The embedded Hermes process is a model runner, not an evolution participant.
It must never inherit a user's session-feed hooks, remote skill-sync hooks, or
teamEvolver ingest environment.  Keeping this policy in one small module lets
the web console, pipeline and helper script enforce the same boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml


EVOLUTION_ENV_VARS = (
    "TEAMEVOLVER_URL",
    "TEAMEVOLVER_USER",
    "TEAMEVOLVER_API_KEY",
    "TEAMEVOLVER_FEED_CONFIG",
    "EVOLVE_INGEST_API_KEY",
    "HERMES_STATE_DB",
    "HERMES_ACCEPT_HOOKS",
)


def sanitize_config_document(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a Hermes config with all external evolution wiring off."""
    sanitized = dict(document or {})

    # The project runtime is non-interactive and purpose-built for SkillMiner.
    # No shell hook is required for mining, while an inherited on_session_end
    # hook can silently upload every oneshot transcript to a remote evolver.
    sanitized.pop("hooks", None)
    sanitized["hooks_auto_accept"] = False

    # A legacy user config may point at ~/.hermes/team_skills and load the
    # teamEvolver sync/feed skills.  SkillMiner deploys only its own three
    # pipeline skills into the isolated home, so external skill roots are not
    # needed here either.
    skills = sanitized.get("skills")
    if isinstance(skills, Mapping):
        clean_skills = dict(skills)
        clean_skills.pop("external_dirs", None)
        if clean_skills:
            sanitized["skills"] = clean_skills
        else:
            sanitized.pop("skills", None)

    return sanitized


def sanitize_config_file(config_path: Path | str) -> bool:
    """Sanitize one existing config atomically; return whether it changed."""
    path = Path(config_path)
    if not path.is_file():
        return False
    loaded = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Hermes 配置必须是 YAML 对象：{path}")
    sanitized = sanitize_config_document(loaded)
    if sanitized == loaded:
        return False

    temporary = path.with_suffix(path.suffix + ".isolation.tmp")
    temporary.write_text(
        yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return True


def sanitize_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child environment that cannot address the evolution service."""
    sanitized = dict(environment if environment is not None else os.environ)
    for name in EVOLUTION_ENV_VARS:
        sanitized.pop(name, None)
    # Defense-in-depth marker for project integrations and future adapters.
    sanitized["TEAMEVOLVER_DISABLE_SESSION_FEED"] = "1"
    return sanitized


def main() -> int:
    """CLI used by scripts/project_hermes.sh before launching Hermes directly."""
    import argparse

    parser = argparse.ArgumentParser(description="isolate SkillMiner's Hermes config")
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    sanitize_config_file(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
