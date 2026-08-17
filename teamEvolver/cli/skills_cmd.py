"""Skill sharing commands: push / pull / sync / list-remote."""

from __future__ import annotations

import os

import click

from ..config_store import ConfigStore


def _sharing_backend(cfg) -> str:
    backend = (
        str(getattr(cfg, "sharing_skill_backend", "") or "").strip().lower()
        or str(getattr(cfg, "sharing_backend", "") or "").strip().lower()
    )
    if backend:
        return "viking"
    if getattr(cfg, "sharing_viking_endpoint", ""):
        return "viking"
    return ""


def _sharing_target(cfg) -> str:
    backend = _sharing_backend(cfg)
    agent = str(getattr(cfg, "sharing_viking_agent_id", "") or "default")
    customer = str(getattr(cfg, "sharing_viking_customer_id", "") or "")
    group = f"{agent}/peers/{customer}" if customer else agent
    if backend == "viking":
        endpoint = getattr(cfg, "sharing_viking_endpoint", "")
        deployment = str(getattr(cfg, "sharing_viking_deployment", "") or "cloud")
        # Wire constant: OpenViking namespace root, part of the shared data
        # Shared contract with AgentsHub and the evolve server.
        root_prefix = (
            getattr(cfg, "sharing_viking_root_prefix", "")
            or "team-skill-evolver"
        )
        return f"OpenViking ({deployment}) storage (resources/{root_prefix} @ {endpoint})"
    return f"{backend} storage ({group})"


def _require_sharing(cs: ConfigStore):
    """Validate that sharing is enabled and configured. Returns (cfg, SkillHub) or raises."""
    cfg = cs.to_config()
    if not cfg.sharing_enabled:
        raise click.ClickException(
            "Skill sharing is not enabled. "
            "Run 'teamEvolver config sharing.enabled true' to configure."
        )
    backend = _sharing_backend(cfg)
    if backend == "viking":
        if not cfg.sharing_viking_endpoint:
            raise click.ClickException(
                "OpenViking sharing backend is not configured. "
                "Set sharing.viking_deployment (cloud or local) or "
                "sharing.viking_endpoint first."
            )
    else:
        raise click.ClickException(
            "Sharing backend is not configured. "
            "Set sharing.viking_deployment to cloud or local."
        )
    from ..skills.hub import SkillHub

    hub = SkillHub.team_from_config(cfg)
    return cfg, hub


@click.group()
def skills():
    """Skill management commands."""


@skills.command(name="push")
@click.option("--no-filter", is_flag=True, help="Skip effectiveness quality gate.")
def skills_push(no_filter):
    """Push local skills to the shared cloud."""
    cs = ConfigStore()
    cfg, hub = _require_sharing(cs)
    click.echo(f"Pushing skills to {_sharing_target(cfg)} ...")
    skill_filter = None
    if not no_filter:
        stats_path = os.path.join(cfg.skills_dir, "skill_stats.json")
        if os.path.exists(stats_path):
            import json

            try:
                with open(stats_path, encoding="utf-8") as f:
                    stats = json.load(f)
                skill_filter = {
                    "stats": stats,
                    "min_injections": cfg.sharing_push_min_injections,
                    "min_effectiveness": cfg.sharing_push_min_effectiveness,
                }
            except Exception:
                pass
    result = hub.push_skills(cfg.skills_dir, skill_filter=skill_filter)
    click.echo(
        f"Done: {result['uploaded']} uploaded, "
        f"{result['skipped']} unchanged, "
        f"{result.get('filtered', 0)} filtered, "
        f"{result.get('submitted', 0)} submitted, "
        f"{result['total_local']} total local skills."
    )


@skills.command(name="pull")
def skills_pull():
    """Pull shared skills from the cloud."""
    cs = ConfigStore()
    cfg, hub = _require_sharing(cs)
    click.echo(f"Pulling skills from {_sharing_target(cfg)} ...")
    result = hub.pull_skills(cfg.skills_dir)
    msg = (
        f"Done: {result['downloaded']} downloaded, "
        f"{result['skipped']} unchanged, "
        f"{result.get('failed', 0)} failed, "
        f"{result.get('deleted', 0)} deleted, "
        f"{result['total_remote']} total remote skills."
    )
    if result.get("failed_names"):
        msg += f" Failed: {', '.join(result.get('failed_names', []))}"
    if result.get("restored_from_backup"):
        msg += f" Restored from backup: {result.get('backup_dir', '')}"
    click.echo(msg)


@skills.command(name="sync")
def skills_sync():
    """Bidirectional sync: pull then push."""
    cs = ConfigStore()
    cfg, hub = _require_sharing(cs)
    click.echo(f"Syncing skills with {_sharing_target(cfg)} ...")
    result = hub.sync_skills(cfg.skills_dir)
    pr = result["pull"]
    ps = result["push"]
    click.echo(
        f"Pull: {pr['downloaded']} downloaded, {pr['skipped']} unchanged\n"
        f"Push: {ps['uploaded']} uploaded, {ps['skipped']} unchanged"
    )


@skills.command(name="list-remote")
def skills_list_remote():
    """List skills available in the shared storage backend."""
    cs = ConfigStore()
    cfg, hub = _require_sharing(cs)
    remote = hub.list_remote()
    if not remote:
        click.echo("No skills found on the cloud.")
        return
    click.echo(f"\n{'=' * 60}")
    click.echo(f"  Shared Skills ({len(remote)} total)")
    click.echo(f"{'=' * 60}\n")
    for rec in sorted(remote, key=lambda r: r.get("name", "")):
        name = rec.get("name", "?")
        desc = rec.get("description", "")
        cat = rec.get("category", "general")
        by = rec.get("uploaded_by", "?")
        at = rec.get("uploaded_at", "?")
        click.echo(f"  {name}  [{cat}]")
        if desc:
            click.echo(f"    {desc}")
        click.echo(f"    by {by}  at {at}")
        click.echo()
