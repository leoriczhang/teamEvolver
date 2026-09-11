#!/usr/bin/env python3
"""Import legacy project YAML and baseline Skill bundles. Dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from teamEvolver.config_store import ConfigStore
from teamEvolver.integrations.agent_registry import register_agent
from teamEvolver.integrations.legacy_converter import inspect_converter
from teamEvolver.skills.bundle import bundle_tree_sha256, read_skill_bundle
from teamEvolver.skills.mutations import SkillMutationCommand, SkillMutationService
from teamEvolver.tenants.registry import TenantRegistry, effective_config, reset_current_tenant, set_current_tenant


def project_overrides(raw: dict, account: str) -> dict:
    lf = raw.get("langfuse") or {}
    llm = raw.get("llm") or {}
    trace_name = lf.get("trace_name") or ""
    if isinstance(trace_name, list):
        trace_name = ",".join(str(name) for name in trace_name)
    return {
        "langfuse_enabled": bool(lf.get("public_key") and lf.get("secret_key")),
        "langfuse_host": str(lf.get("host") or ""),
        "langfuse_public_key": str(lf.get("public_key") or ""),
        "langfuse_secret_key": str(lf.get("secret_key") or ""),
        "langfuse_default_trace_name": trace_name,
        "llm_api_base": str(llm.get("url") or ""),
        "llm_api_key": str(llm.get("token") or ""),
        "llm_model_id": str(llm.get("model") or ""),
        "llm_max_tokens": int(llm.get("max_tokens") or 8192),
        "sharing_session_backend": "postgres",
        "sharing_skill_backend": "postgres",
        "evolve_publish_mode": "validated",
        "evolve_drain_max_per_cycle": 100,
        "langfuse_mappers": [
            {
                "id": "customer-runtime",
                "name": "Customer runtime",
                "enabled": True,
                "code": (
                    "def map_session(converted, session, traces):\n"
                    f"    return {{'runtime': {{'type': 'deap', 'integration_id': {account!r}}}}}\n"
                ),
            }
        ],
    }


def scan_converters(directory: Path) -> list[dict]:
    return [
        {"file": path.name, **inspect_converter(path.read_text(encoding="utf-8"))}
        for path in sorted(directory.glob("*.py"))
    ]


def converter_preflight(directory: Path, project: str, raw: dict, *, allow_doris_api_fallback=False):
    directory = directory.resolve()
    path = None
    for name in ((raw.get("langfuse") or {}).get("project_id"), project):
        if not name:
            continue
        candidate = (directory / (str(name) + ".py")).resolve()
        if candidate.parent != directory:
            raise ValueError("converter name escapes converters directory")
        if candidate.is_file():
            path = candidate
            break
    code = path.read_text(encoding="utf-8") if path else ""
    result = inspect_converter(code)
    result["file"] = path.name if path else ""
    if path is None:
        result["issues"].append("missing converter; supply --converters-dir pointing to legacy source/converters")
    if (raw.get("langfuse") or {}).get("project_id") and not allow_doris_api_fallback:
        result["issues"].append(
            "Doris source requires explicit --allow-doris-api-fallback after API parity verification, or a custom SourceAdapter"
        )
    result["status"] = "blocked" if result["issues"] else "compatible"
    return code, result


def import_projects(
    root: Path, *, apply=False, project="", account_id="", converters_dir=None, allow_doris_api_fallback=False
) -> list[dict]:
    config = ConfigStore().to_config() if apply else None
    registry = TenantRegistry(config) if apply else None
    converters_dir = Path(converters_dir) if converters_dir else root / "converters"
    paths = sorted((root / "config").glob("*.yaml"))
    if project:
        paths = [path for path in paths if path.stem == project]
    if not paths:
        raise ValueError("No project YAML found; supply the customer's DATA_ROOT (not just the source code)")
    if account_id and len(paths) != 1:
        raise ValueError("--account-id requires exactly one --project")
    report = []
    prepared = []
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid project YAML: {path.name}")
        code, result = converter_preflight(
            converters_dir, path.stem, raw, allow_doris_api_fallback=allow_doris_api_fallback
        )
        prepared.append((path, raw, code, result))
    if apply and any(item[3]["issues"] for item in prepared):
        names = ", ".join(path.stem for path, _, _, check in prepared if check["issues"])
        raise ValueError(f"Migration preflight blocked: {names}. Run without --apply for details. No data changed.")
    for path, raw, code, converter in prepared:
        account = account_id or "skillopt-" + hashlib.sha256(path.stem.encode()).hexdigest()[:16]
        workspace = root / "claw_workspaces" / path.stem / "workspace" / "skills"
        bundles = sorted(workspace.glob("*/SKILL.md"))
        warnings = [
            "Legacy prompts, schedules, CAS, Doris writeback and reports require separate acceptance",
        ]
        if (raw.get("langfuse") or {}).get("project_id"):
            warnings.append("Doris project_id is not migrated; Langfuse public API is used")
        row = {
            "project": path.stem,
            "account_id": account,
            "skills": len(bundles),
            "warnings": warnings,
            "converter": converter,
            "status": converter["status"],
        }
        if apply:
            if not config.storage_pg_enabled:
                raise ValueError("Import requires PostgreSQL storage")
            ctx = registry.get(account)
            if ctx is None:
                ctx, _ = registry.create_tenant(path.stem, account)
            if ctx.status != "active":
                raise ValueError("Cannot import into disabled account")
            overrides = project_overrides(raw, account)
            overrides.update(
                datasource_type="skillopt",
                datasource_legacy_project=path.stem,
                datasource_legacy_converter_code=code,
                datasource_legacy_options={
                    "cron_dedup": bool((raw.get("analyze") or {}).get("cron_dedup", False)),
                    "max_traces": 10000,
                },
            )
            ctx = registry.update_tenant_config(account, overrides)
            scoped = effective_config(registry, ctx, config)
            token = set_current_tenant(ctx)
            try:
                service = SkillMutationService.from_config(scoped, tenant_id=account)
                for entry in bundles:
                    files = read_skill_bundle(entry.parent)
                    digest = bundle_tree_sha256(files)
                    service.execute(
                        SkillMutationCommand(
                            action="publish",
                            name=entry.parent.name,
                            skills_dir=str(workspace),
                            mutation_id="import-"
                            + hashlib.sha256(f"{account}:{entry.parent.name}:{digest}".encode()).hexdigest(),
                        )
                    )
                experiment = raw.get("experiment") or {}
                endpoint = str(experiment.get("agent_host") or "").rstrip("/")
                if endpoint:
                    register_agent(
                        scoped,
                        {
                            "schema_version": "teamevolver.agent-registration.v1",
                            "protocol_version": "1.0",
                            "agent_id": account,
                            "runtime_type": "deap",
                            "runtime_version": "1.0.0",
                            "display_name": path.stem,
                            "capabilities": {
                                "replay.branch.v1": {
                                    "transport": "deap",
                                    "orchestration": "server_driven",
                                    "endpoint": endpoint,
                                    "employee_no": str(experiment.get("emp_id") or ""),
                                }
                            },
                            "endpoints": {"replay_url": endpoint},
                        },
                    )
            finally:
                reset_current_tenant(token)
            row["applied"] = True
        report.append(row)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--converters-dir", type=Path)
    parser.add_argument("--scan-converters", action="store_true")
    parser.add_argument("--allow-doris-api-fallback", action="store_true")
    parser.add_argument("--project", default="")
    parser.add_argument("--account-id", default="", help="Existing OpenViking Account ID (single project)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.scan_converters:
        if args.converters_dir is None:
            parser.error("--scan-converters requires --converters-dir")
        print(json.dumps(scan_converters(args.converters_dir), ensure_ascii=False, indent=2))
        return
    if args.legacy_root is None:
        parser.error("--legacy-root is required for project migration")
    print(
        json.dumps(
            import_projects(
                args.legacy_root,
                apply=args.apply,
                project=args.project,
                account_id=args.account_id,
                converters_dir=args.converters_dir,
                allow_doris_api_fallback=args.allow_doris_api_fallback,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
