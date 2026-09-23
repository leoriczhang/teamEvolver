#!/usr/bin/env python3
"""Prepare a customer runtime and run migration preflight. No legacy files are changed."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import tempfile
from pathlib import Path

from import_skillopt import import_projects

from teamEvolver.config_store import ConfigStore
from teamEvolver.storage.pg_pool import close_pg_runtimes, dsn_from_env
from teamEvolver.tenants.registry import TenantRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-root", type=Path, required=True, help="Old DATA_ROOT containing config/ and claw_workspaces/"
    )
    parser.add_argument("--converters-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime/customer"))
    parser.add_argument("--allow-doris-api-fallback", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Import projects after all preflight checks pass")
    args = parser.parse_args()
    root = args.runtime_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_path = root / "customer.env"
    previous = (
        dict(token.split("=", 1) for token in shlex.split(env_path.read_text(), comments=True) if "=" in token)
        if env_path.exists()
        else {}
    )
    dsn = os.environ.get("TEAMEVOLVER_PG_DSN") or dsn_from_env() or previous.get("TEAMEVOLVER_PG_DSN")
    if not dsn:
        parser.exit(2, "Set TEAMEVOLVER_PG_DSN or OV_PG_* database variables first.\n")
    root_key = (
        os.environ.get("TEAMEVOLVER_ROOT_API_KEY") or previous.get("TEAMEVOLVER_ROOT_API_KEY") or secrets.token_hex(32)
    )
    if len(root_key) < 32:
        parser.exit(2, "Root key must have at least 32 characters.\n")
    skill_backend = str(
        os.environ.get("TEAMEVOLVER_SKILL_STORAGE_BACKEND")
        or previous.get("TEAMEVOLVER_SKILL_STORAGE_BACKEND")
        or "local"
    ).strip().lower()
    if skill_backend not in {"local", "viking"}:
        parser.exit(2, "TEAMEVOLVER_SKILL_STORAGE_BACKEND must be local or viking.\n")
    host_skill_root = Path(
        os.environ.get("SKILL_STORAGE_ROOT")
        or previous.get("SKILL_STORAGE_ROOT")
        or root.parent / "skill-storage"
    ).expanduser().resolve()
    skill_storage_root = str(
        Path(
            os.environ.get("TEAMEVOLVER_SKILL_STORAGE_ROOT")
            or previous.get("TEAMEVOLVER_SKILL_STORAGE_ROOT")
            or host_skill_root
        ).expanduser().resolve()
    )
    if skill_backend == "local":
        host_skill_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = root / "config.yaml"
    config_store = ConfigStore(config_path)
    if not config_path.exists():
        template = Path(__file__).resolve().parents[1] / "docker/customer.yaml"
        config_store.save(ConfigStore(template).load())
    env = {
        **{key: value for key, value in previous.items() if value is not None},
        "DATA_ROOT": str(root),
        "TEAMEVOLVER_CONFIG_FILE": str(config_path),
        "TEAMEVOLVER_PG_DSN": dsn,
        "TEAMEVOLVER_ROOT_API_KEY": root_key,
        "TEAMEVOLVER_UID": str(os.getuid()),
        "TEAMEVOLVER_GID": str(os.getgid()),
        "TEAMEVOLVER_SKILL_STORAGE_BACKEND": skill_backend,
        "TEAMEVOLVER_SKILL_STORAGE_ROOT": skill_storage_root,
        "SKILL_STORAGE_ROOT": str(host_skill_root),
    }
    for key in (
        "TEAMEVOLVER_LLM_API_KEY",
        "TEAMEVOLVER_LLM_BASE_URL",
        "TEAMEVOLVER_LLM_MODEL",
        "TEAMEVOLVER_OV_ENDPOINT",
        "TEAMEVOLVER_OV_ROOT_KEY",
        "TEAMEVOLVER_LLM_MAX_OUTPUT_TOKENS",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".customer-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, env_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    os.chmod(env_path, 0o600)
    os.environ.update(env)
    try:
        status = TenantRegistry(config_store.to_config()).runtime.pool_status()
        if not status.get("reachable"):
            parser.exit(
                2,
                f"PostgreSQL check failed: {status.get('reason')}. "
                "Check network, database and NOSUPERUSER/NOBYPASSRLS role.\n",
            )
        report = import_projects(
            args.legacy_root.resolve(),
            converters_dir=args.converters_dir.resolve(),
            apply=args.apply,
            allow_doris_api_fallback=args.allow_doris_api_fallback,
        )
        report_path = root / "migration-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        blocked = [item["project"] for item in report if item.get("status") == "blocked"]
        print(
            json.dumps(
                {
                    "database": "ready",
                    "projects": len(report),
                    "blocked": blocked,
                    "applied": args.apply,
                    "report": str(report_path),
                    "environment_file": str(env_path),
                },
                ensure_ascii=False,
            )
        )
        if blocked:
            return 2
        return 0
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f"Migration not completed: {exc}\n")
    finally:
        close_pg_runtimes()


if __name__ == "__main__":
    raise SystemExit(main())
