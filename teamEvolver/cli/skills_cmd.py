"""Skill registry inspection and runtime-cache materialization commands."""

from __future__ import annotations

import click

from ..config_store import ConfigStore


def _sharing_backend(cfg) -> str:
    from ..storage import normalize_backend

    configured = str(
        getattr(cfg, "sharing_skill_backend", "") or ""
    ).strip().lower()
    backend = normalize_backend(
        configured,
        endpoint=str(getattr(cfg, "sharing_viking_endpoint", "") or ""),
    )
    if backend:
        return backend
    if getattr(cfg, "sharing_viking_endpoint", ""):
        return "viking"
    return "local"


def _sharing_target(cfg) -> str:
    backend = _sharing_backend(cfg)
    if backend == "viking":
        endpoint = getattr(cfg, "sharing_viking_endpoint", "")
        deployment = str(getattr(cfg, "sharing_viking_deployment", "") or "cloud")
        # Wire constant: OpenViking namespace root shared with the evolve server.
        root_prefix = (
            getattr(cfg, "sharing_viking_root_prefix", "")
            or "team-skill-evolver"
        )
        return f"OpenViking ({deployment}) storage (resources/{root_prefix} @ {endpoint})"
    if backend == "local":
        root = (
            str(getattr(cfg, "sharing_skill_local_root", "") or "")
            or str(getattr(cfg, "sharing_local_root", "") or "")
            or "~/.teamEvolver/local_store"
        )
        return f"local/NAS storage ({root})"
    if backend == "postgres":
        schema = str(getattr(cfg, "storage_pg_schema", "") or "teamevolver")
        return f"PostgreSQL storage (schema={schema})"
    return f"{backend} storage"


def _build_skill_registry(cs: ConfigStore):
    """Validate and build the configured Skill registry."""
    cfg = cs.to_config()
    backend = _sharing_backend(cfg)
    if backend == "viking":
        if not cfg.sharing_viking_endpoint:
            raise click.ClickException(
                "OpenViking sharing backend is not configured. "
                "Set sharing.viking_deployment (cloud or local) or "
                "sharing.viking_endpoint first."
            )
    elif backend == "postgres":
        if not bool(getattr(cfg, "storage_pg_enabled", False)):
            raise click.ClickException(
                "PostgreSQL Skill storage requires storage_pg.enabled=true."
            )
    elif backend != "local":
        raise click.ClickException(
            "Sharing backend is not configured. "
            "Set sharing.skill_backend to local, postgres, or viking."
        )
    from team_skills.library.hub import SkillHub

    hub = SkillHub.team_from_config(cfg)
    return cfg, hub


@click.group()
def skills():
    """Skill management commands."""


@skills.command(name="pull")
def skills_pull():
    """Materialize the durable Skill registry into the runtime cache."""
    cs = ConfigStore()
    cfg, hub = _build_skill_registry(cs)
    click.echo(f"Materializing skills from {_sharing_target(cfg)} ...")
    result = hub.materialize_runtime_cache(cfg.skills_runtime_cache_dir)
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


@skills.command(name="list")
def skills_list():
    """List Skills in the authoritative tenant registry."""
    cs = ConfigStore()
    _cfg, hub = _build_skill_registry(cs)
    remote = hub.list_skills()
    if not remote:
        click.echo("No Skills found in the registry.")
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
