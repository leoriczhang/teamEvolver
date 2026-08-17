"""Reset module — clears all local state and optionally remote team memory.

Usage:
    dreamcycle --reset              # Clear local state only
    dreamcycle --reset --remote     # Clear local state + remote team space
    dreamcycle --reset --dry-run    # Show what would be cleared
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from .config import DreamCycleConfig

logger = logging.getLogger(__name__)


def _collect_local_paths(config: DreamCycleConfig) -> List[Path]:
    """Collect all local paths that would be cleared."""
    paths: List[Path] = []

    # state.json
    state_file = config.log.state_file
    if state_file.exists():
        paths.append(state_file)

    # Log files
    log_dir = config.log.log_dir
    if log_dir.exists():
        for f in log_dir.iterdir():
            if f.is_file():
                paths.append(f)

    # Report files
    report_dir = config.log.report_dir
    if report_dir.exists():
        for f in report_dir.iterdir():
            if f.is_file():
                paths.append(f)

    return paths


def _clear_local(config: DreamCycleConfig, dry_run: bool = False) -> List[str]:
    """Clear all local state (state.json, logs, reports).

    Returns a list of actions taken.
    """
    actions: List[str] = []

    # 1. Delete state.json
    state_file = config.log.state_file
    if state_file.exists():
        if dry_run:
            actions.append(f"[DRY-RUN] Would delete: {state_file}")
        else:
            state_file.unlink()
            actions.append(f"Deleted: {state_file}")

    # 2. Clear log files
    log_dir = config.log.log_dir
    if log_dir.exists():
        for f in sorted(log_dir.iterdir()):
            if f.is_file():
                if dry_run:
                    actions.append(f"[DRY-RUN] Would delete: {f}")
                else:
                    f.unlink()
                    actions.append(f"Deleted: {f}")

    # 3. Clear report files
    report_dir = config.log.report_dir
    if report_dir.exists():
        for f in sorted(report_dir.iterdir()):
            if f.is_file():
                if dry_run:
                    actions.append(f"[DRY-RUN] Would delete: {f}")
                else:
                    f.unlink()
                    actions.append(f"Deleted: {f}")

    if not actions:
        actions.append("Nothing to clear — local state is already empty.")

    return actions


def _clear_remote_maintained_space(config: DreamCycleConfig, dry_run: bool = False) -> List[str]:
    """Clear all memories from the authenticated user's own OpenViking memory space.

    Lists all .md files under the maintained user-memory root and archives them.
    Returns a list of actions taken.
    """
    import httpx

    from .tools.viking import _archived_uri, maintained_space_root

    actions: List[str] = []
    customer_id = config.viking.customer_id
    user = config.viking.agent_id
    root = maintained_space_root(customer_id)
    endpoint = config.viking.endpoint.rstrip("/")

    headers = {
        "X-OpenViking-Account": config.viking.account,
        "X-OpenViking-User": user,
    }
    if config.viking.agent:
        headers["X-OpenViking-Agent"] = config.viking.agent
    if config.viking.api_key:
        headers["X-API-Key"] = config.viking.api_key

    try:
        client = httpx.Client(headers=headers, timeout=30.0)

        # List all files recursively
        list_resp = client.get(
            f"{endpoint}/api/v1/fs/ls",
            params={"uri": root, "recursive": "true", "node_limit": 2000},
        )
        if list_resp.status_code != 200:
            actions.append(
                f"Failed to list remote files: HTTP {list_resp.status_code}: {list_resp.text[:200]}"
            )
            return actions

        data = list_resp.json()
        entries = data.get("result", [])

        # Collect all .md file URIs (skip anything already under _archived/)
        md_uris: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri") or ""
            name = entry.get("name") or uri.rsplit("/", 1)[-1]
            if uri.endswith(".md") or name.endswith(".md"):
                if not uri.startswith("viking://"):
                    uri = f"{root.rstrip('/')}/{name}"
                if "/memories/_archived/" in uri:
                    continue
                md_uris.append(uri)

        if not md_uris:
            actions.append("Remote user-memory space is already empty — no .md files found.")
            return actions

        # Archive each file by moving to _archived/
        for uri in sorted(md_uris):
            archived_uri = _archived_uri(uri)
            if dry_run:
                actions.append(f"[DRY-RUN] Would archive: {uri} → {archived_uri}")
            else:
                mv_resp = client.post(
                    f"{endpoint}/api/v1/fs/mv",
                    json={"from_uri": uri, "to_uri": archived_uri},
                )
                if mv_resp.status_code == 200:
                    actions.append(f"Archived: {uri}")
                elif mv_resp.status_code == 404:
                    actions.append(f"Already gone: {uri}")
                else:
                    actions.append(
                        f"FAILED to archive {uri}: HTTP {mv_resp.status_code}: {mv_resp.text[:100]}"
                    )

    except Exception as e:
        actions.append(f"Remote reset error: {e}")

    return actions


def reset(
    config: DreamCycleConfig,
    *,
    remote: bool = False,
    dry_run: bool = False,
) -> str:
    """Execute a full reset.

    Args:
        config: DreamCycle configuration.
        remote: If True, also clear remote OpenViking team space.
        dry_run: If True, only show what would be done.

    Returns:
        A summary string of all actions taken.
    """
    lines: List[str] = []

    mode = "DRY-RUN" if dry_run else "RESET"
    scope = "local + remote" if remote else "local only"
    lines.append(f"=== DreamCycle {mode} ({scope}) ===")
    lines.append("")

    # 1. Clear local state
    lines.append("--- Local State ---")
    local_actions = _clear_local(config, dry_run=dry_run)
    for action in local_actions:
        lines.append(f"  {action}")
    lines.append(f"  → {len(local_actions)} local item(s)")
    lines.append("")

    # 2. Optionally clear remote
    if remote:
        lines.append("--- Remote Maintained Space ---")
        remote_actions = _clear_remote_maintained_space(config, dry_run=dry_run)
        for action in remote_actions:
            lines.append(f"  {action}")
        lines.append(f"  → {len(remote_actions)} remote action(s)")
        lines.append("")

    lines.append("=== Reset complete ===")
    return "\n".join(lines)
