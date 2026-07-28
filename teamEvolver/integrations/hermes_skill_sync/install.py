#!/usr/bin/env python3
"""Install the teamEvolver-sync Hermes integration.

This installs a ``pre_llm_call`` shell hook that pulls team teamEvolver skills
into a Hermes-visible directory before each LLM turn. It does not modify Hermes
model settings and does not route LLM requests through teamEvolver.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teamEvolver.config import VOLCENGINE_OPENVIKING_ENDPOINT

SKILL_NAME = "teamEvolver-sync"
BUNDLE_FILES = ("SKILL.md", "sync_skills.py")
ALLOWLIST_FILENAME = "shell-hooks-allowlist.json"
HOOK_EVENT = "pre_llm_call"


def _hermes_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text("utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        raise SystemExit(
            "PyYAML not available but Hermes config.yaml exists; install pyyaml "
            "or add hooks and skills.external_dirs manually."
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to parse {path}: {exc}")


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")
    os.replace(tmp, path)


def _wire_hook(config_path: Path, command: str, timeout: int) -> str:
    data = _load_yaml(config_path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    entries = hooks.get(HOOK_EVENT)
    if not isinstance(entries, list):
        entries = []
    target_script = _command_script(command)
    matching = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and _command_script(str(entry.get("command", ""))) == target_script
    ]
    if (
        len(matching) == 1
        and str(matching[0].get("command", "")).strip() == command
        and int(matching[0].get("timeout") or timeout) == timeout
    ):
        return "already-present"
    entries = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and _command_script(str(entry.get("command", ""))) == target_script
        )
    ]
    entries.append({"command": command, "timeout": timeout})
    hooks[HOOK_EVENT] = entries
    data["hooks"] = hooks
    _dump_yaml(config_path, data)
    return "updated" if matching else "added"


def _command_script(command: str) -> str:
    try:
        parts = shlex.split(str(command or ""))
    except ValueError:
        parts = str(command or "").split()
    return os.path.realpath(os.path.expanduser(parts[-1])) if parts else ""


def _wire_external_dir(config_path: Path, target_dir: Path) -> str:
    data = _load_yaml(config_path)
    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    external_dirs = skills.get("external_dirs")
    if isinstance(external_dirs, str):
        external_dirs = [external_dirs]
    if not isinstance(external_dirs, list):
        external_dirs = []
    target = str(target_dir.expanduser())
    if target in [str(item) for item in external_dirs]:
        return "already-present"
    external_dirs.append(target)
    skills["external_dirs"] = external_dirs
    data["skills"] = skills
    _dump_yaml(config_path, data)
    return "added"


def _script_mtime_iso(command: str) -> str | None:
    parts = command.split()
    script = parts[-1] if parts else command
    try:
        ts = os.path.getmtime(os.path.expanduser(script))
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _approve_hook(home: Path, command: str) -> str:
    path = home / ALLOWLIST_FILENAME
    try:
        raw = json.loads(path.read_text("utf-8"))
        approvals = raw.get("approvals")
        if not isinstance(approvals, list):
            approvals = []
    except (OSError, ValueError):
        raw, approvals = {}, []

    target_script = _command_script(command)

    def _matches(entry: object) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("event") == HOOK_EVENT
            and _command_script(str(entry.get("command") or "")) == target_script
        )

    already = any(_matches(entry) for entry in approvals)
    approvals = [entry for entry in approvals if not _matches(entry)]
    approvals.append(
        {
            "event": HOOK_EVENT,
            "command": command,
            "approved_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "script_mtime_at_approval": _script_mtime_iso(command),
        }
    )
    raw["approvals"] = approvals
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)
    return "re-approved" if already else "approved"


def _write_sync_config(path: Path, args, target_dir: Path) -> None:
    payload: dict[str, Any] = {
        "target_dir": str(target_dir),
        "min_interval_seconds": args.min_interval_seconds,
        "mirror": bool(args.mirror),
    }
    optional_fields = {
        "backend": args.backend,
        "base_url": args.url or args.service_url,
        "user_alias": args.user,
        "api_key": args.api_key,
        "local_root": args.local_root,
    }
    if args.backend == "viking":
        optional_fields.update(
            {
                "viking_endpoint": args.viking_endpoint,
                "viking_team_api_key": args.viking_team_api_key,
                "viking_api_key": args.viking_api_key,
                "viking_account": args.viking_account,
                "viking_user": args.viking_user,
                "viking_agent": args.viking_agent,
                "viking_agent_id": args.viking_agent_id,
                "viking_customer_id": args.viking_customer_id,
                "viking_root_prefix": args.viking_root_prefix,
                "viking_group_id": args.viking_group_id,
            }
        )
    for key, value in optional_fields.items():
        if value not in (None, ""):
            payload[key] = value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the teamEvolver-sync Hermes hook")
    parser.add_argument("--hermes-home", default=None, help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    parser.add_argument("--python", default="python3", help="interpreter used in the hook command")
    parser.add_argument("--timeout", type=int, default=60, help="hook timeout seconds (default 60)")
    parser.add_argument("--target-dir", default="", help="team skill sync dir (default <hermes-home>/team_skills/teamEvolver)")
    parser.add_argument("--min-interval-seconds", type=int, default=60, help="minimum pull interval per Hermes process")
    parser.add_argument("--mirror", action="store_true", help="delete local team skills absent from remote manifest")
    parser.add_argument("--no-hook", action="store_true", help="install files and config only, skip hook wiring")
    parser.add_argument("--no-approve", action="store_true", help="skip scoped allowlist approval")
    parser.add_argument(
        "--backend",
        default="service",
        choices=["service", "viking", "local"],
        help="team skill backend (default: teamEvolver service; no local OpenViking key needed)",
    )
    parser.add_argument("--url", default="", help="teamEvolver service URL for --backend service")
    parser.add_argument("--service-url", default="", help="alias of --url")
    parser.add_argument("--user", default="", help="teamEvolver user alias for service sync attribution")
    parser.add_argument("--api-key", default="", help="optional teamEvolver service sync API key")
    parser.add_argument(
        "--viking-endpoint",
        default=VOLCENGINE_OPENVIKING_ENDPOINT,
        help="OpenViking endpoint",
    )
    parser.add_argument("--viking-team-api-key", default="", help="team OpenViking API key")
    parser.add_argument("--viking-api-key", default="", help="fallback OpenViking API key")
    parser.add_argument("--viking-account", default="", help="OpenViking account")
    parser.add_argument("--viking-user", default="", help="OpenViking user")
    parser.add_argument("--viking-agent", default="", help="OpenViking agent namespace")
    parser.add_argument("--viking-agent-id", default="", help="OpenViking agent id")
    parser.add_argument("--viking-customer-id", default="", help="optional per-customer prefix")
    parser.add_argument("--viking-root-prefix", default="", help="OpenViking resources root prefix")
    parser.add_argument("--viking-group-id", default="", help="optional OpenViking group segment")
    parser.add_argument("--local-root", default="", help="local object-store root when --backend local")
    args = parser.parse_args(argv)

    src_dir = Path(__file__).resolve().parent
    home = _hermes_home(args.hermes_home)
    dst_dir = home / "skills" / SKILL_NAME
    target_dir = Path(args.target_dir).expanduser() if args.target_dir else home / "team_skills" / "teamEvolver"
    dst_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in BUNDLE_FILES:
        src = src_dir / name
        if not src.exists():
            raise SystemExit(f"missing bundle file: {src}")
        shutil.copy2(src, dst_dir / name)
    print(f"[install] copied integration -> {dst_dir}")

    _write_sync_config(dst_dir / "sync.json", args, target_dir)
    print(f"[install] wrote sync.json (target={target_dir})")

    external_status = _wire_external_dir(home / "config.yaml", target_dir)
    print(f"[install] skills.external_dirs: {external_status} -> {target_dir}")

    if args.no_hook:
        print("[install] --no-hook: skipped config.yaml hook wiring")
    else:
        command = f"{args.python} {dst_dir / 'sync_skills.py'}"
        hook_status = _wire_hook(home / "config.yaml", command, args.timeout)
        print(f"[install] {HOOK_EVENT} hook: {hook_status} -> {command}")
        if args.no_approve:
            print("[install] --no-approve: hook will need a one-time TTY approval on first fire")
        else:
            approval = _approve_hook(home, command)
            print(f"[install] allowlist approval: {approval} (scoped to this one hook)")

    print("\nDone. Next:")
    print(f"  {args.python} {dst_dir / 'sync_skills.py'}   # dry pull now")
    print("  hermes hooks test pre_llm_call               # verify hook wiring")
    print("  hermes hooks list                            # confirm approval status")
    print("  /reload-skills                               # in a running Hermes session, refresh skill caches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
