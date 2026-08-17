"""Langfuse integration commands: status / list / pull.

``status`` and ``list`` run in-process against the Langfuse public API (no
teamEvolver service required). ``pull`` posts to a running teamEvolver service
so pulled sessions flow through the same ingest + evolve-trigger pipeline as
``/ingest_session``.
"""

from __future__ import annotations

import json

import click

from ..config_store import ConfigStore


def _require_langfuse(cs: ConfigStore, *, pull_required: bool = True):
    cfg = cs.to_config()
    enabled = bool(getattr(cfg, "langfuse_enabled", False))
    tracing_enabled = bool(
        getattr(cfg, "langfuse_tracing_enabled", False)
    )
    if (pull_required and not enabled) or (
        not pull_required and not enabled and not tracing_enabled
    ):
        raise click.ClickException(
            "Langfuse integration is disabled. Enable it with "
            "'teamEvolver config langfuse.enabled true' or "
            "'teamEvolver config langfuse.tracing_enabled true', then set "
            "langfuse.host / langfuse.public_key / langfuse.secret_key."
        )
    if not getattr(cfg, "langfuse_public_key", "") or not getattr(cfg, "langfuse_secret_key", ""):
        raise click.ClickException(
            "Langfuse credentials are missing. Set langfuse.public_key and "
            "langfuse.secret_key."
        )
    return cfg


def _overrides_from_options(
    *,
    environment: tuple[str, ...],
    user_id: str,
    tags: tuple[str, ...],
    release: str,
    version: str,
    trace_name: str,
    session_id: str,
    from_timestamp: str,
    to_timestamp: str,
    metadata: tuple[str, ...],
) -> dict:
    overrides: dict = {}
    if environment:
        overrides["environment"] = list(environment)
    if user_id:
        overrides["user_id"] = user_id
    if tags:
        overrides["tags"] = list(tags)
    if release:
        overrides["release"] = release
    if version:
        overrides["version"] = version
    if trace_name:
        overrides["trace_name"] = trace_name
    if session_id:
        overrides["session_id"] = session_id
    if from_timestamp:
        overrides["from_timestamp"] = from_timestamp
    if to_timestamp:
        overrides["to_timestamp"] = to_timestamp
    if metadata:
        parsed: dict[str, str] = {}
        for item in metadata:
            if "=" not in item:
                raise click.ClickException(
                    f"--metadata expects key=value, got: {item!r}"
                )
            key, _, value = item.partition("=")
            key = key.strip()
            if key:
                parsed[key] = value.strip()
        if parsed:
            overrides["metadata"] = parsed
    return overrides


def _service_base_url(cs: ConfigStore) -> str:
    port = int(cs.get("service.port") or cs.get("proxy.port") or 52010)
    return f"http://127.0.0.1:{port}"


# Shared filter options applied to `list` and `pull`.
def _filter_options(func):
    func = click.option(
        "--environment",
        "-e",
        multiple=True,
        help="Filter by environment (repeatable).",
    )(func)
    func = click.option("--user-id", "-u", default="", help="Filter by trace userId.")(func)
    func = click.option(
        "--tag",
        "tags",
        multiple=True,
        help="Filter by trace tag (repeatable; all must match).",
    )(func)
    func = click.option("--release", default="", help="Filter by trace release.")(func)
    func = click.option("--version", default="", help="Filter by trace version.")(func)
    func = click.option("--name", "trace_name", default="", help="Filter by trace name.")(func)
    func = click.option("--session-id", default="", help="Pull one specific session id.")(func)
    func = click.option(
        "--from",
        "from_timestamp",
        default="",
        help="Only sessions on/after this ISO 8601 datetime.",
    )(func)
    func = click.option(
        "--to",
        "to_timestamp",
        default="",
        help="Only sessions before this ISO 8601 datetime.",
    )(func)
    func = click.option(
        "--metadata",
        "-m",
        multiple=True,
        help="Filter by trace metadata key=value (repeatable).",
    )(func)
    func = click.option(
        "--max-sessions",
        type=int,
        default=0,
        help="Cap number of sessions (0 = use config default).",
    )(func)
    return func


@click.group()
def langfuse():
    """Langfuse session ingestion commands."""


@langfuse.command(name="status")
def langfuse_status():
    """Check Langfuse connectivity and configured default filters."""
    cs = ConfigStore()
    cfg = _require_langfuse(cs, pull_required=False)
    from ..integrations.langfuse_client import LangfuseClient, LangfuseError
    from ..observability import configure_langfuse

    click.echo(f"host: {cfg.langfuse_host}")
    click.echo(f"session_pull_enabled: {cfg.langfuse_enabled}")
    click.echo(f"tracing_enabled: {cfg.langfuse_tracing_enabled}")
    click.echo(f"tracing_environment: {cfg.langfuse_tracing_environment}")
    click.echo(f"public_key: {'present' if cfg.langfuse_public_key else 'missing'}")
    click.echo(f"secret_key: {'present' if cfg.langfuse_secret_key else 'missing'}")
    click.echo(f"max_sessions: {cfg.langfuse_max_sessions}")
    click.echo(f"default.environment: {','.join(cfg.langfuse_default_environment) or '(any)'}")
    click.echo(f"default.user_id: {cfg.langfuse_default_user_id or '(any)'}")
    click.echo(f"default.tags: {','.join(cfg.langfuse_default_tags) or '(any)'}")
    tracing = configure_langfuse(cfg)
    click.echo(f"tracing_sdk_available: {tracing['sdk_available']}")
    try:
        health = LangfuseClient.from_config(cfg).health()
    except LangfuseError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("reachable: True")
    total = health.get("total_sessions")
    if total is not None:
        click.echo(f"total_sessions: {total}")


@langfuse.command(name="list")
@_filter_options
def langfuse_list(
    environment: tuple[str, ...],
    user_id: str,
    tags: tuple[str, ...],
    release: str,
    version: str,
    trace_name: str,
    session_id: str,
    from_timestamp: str,
    to_timestamp: str,
    metadata: tuple[str, ...],
    max_sessions: int,
):
    """List matching Langfuse sessions (no ingestion)."""
    cs = ConfigStore()
    cfg = _require_langfuse(cs)
    from ..integrations.langfuse_pull import preview_sessions

    overrides = _overrides_from_options(
        environment=environment,
        user_id=user_id,
        tags=tags,
        release=release,
        version=version,
        trace_name=trace_name,
        session_id=session_id,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        metadata=metadata,
    )
    from ..integrations.langfuse_client import LangfuseError

    try:
        result = preview_sessions(cfg, overrides, max_sessions=max_sessions)
    except LangfuseError as exc:
        raise click.ClickException(str(exc)) from None

    sessions = result.get("sessions") or []
    click.echo(f"Matched {result.get('count', len(sessions))} session(s):\n")
    for item in sessions:
        sid = item.get("session_id", "?")
        title = item.get("title") or ""
        user = item.get("user_id") or ""
        env = item.get("environment") or ""
        tcount = item.get("trace_count")
        tag_list = ",".join(item.get("tags") or [])
        click.echo(f"  {sid}")
        meta_bits = []
        if title:
            meta_bits.append(f"title={title[:60]}")
        if user:
            meta_bits.append(f"user={user}")
        if env:
            meta_bits.append(f"env={env}")
        if tcount is not None:
            meta_bits.append(f"traces={tcount}")
        if tag_list:
            meta_bits.append(f"tags={tag_list}")
        if meta_bits:
            click.echo(f"    {'  '.join(meta_bits)}")
    if not sessions:
        click.echo("  (none)")


@langfuse.command(name="pull")
@_filter_options
@click.option("--user-alias", default="", help="user_alias attributed to pulled sessions.")
@click.option("--force", is_flag=True, help="Reprocess even if content is unchanged.")
@click.option(
    "--defer-trigger",
    is_flag=True,
    help="Queue sessions without scheduling an evolve trigger.",
)
@click.option(
    "--in-process",
    is_flag=True,
    help="Ingest locally instead of posting to the running service.",
)
def langfuse_pull(
    environment: tuple[str, ...],
    user_id: str,
    tags: tuple[str, ...],
    release: str,
    version: str,
    trace_name: str,
    session_id: str,
    from_timestamp: str,
    to_timestamp: str,
    metadata: tuple[str, ...],
    max_sessions: int,
    user_alias: str,
    force: bool,
    defer_trigger: bool,
    in_process: bool,
):
    """Pull matching Langfuse sessions into the evolution pipeline."""
    cs = ConfigStore()
    cfg = _require_langfuse(cs)
    overrides = _overrides_from_options(
        environment=environment,
        user_id=user_id,
        tags=tags,
        release=release,
        version=version,
        trace_name=trace_name,
        session_id=session_id,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        metadata=metadata,
    )

    if in_process:
        result = _pull_in_process(
            cfg,
            overrides,
            max_sessions=max_sessions,
            user_alias=user_alias,
            force=force,
            defer_trigger=defer_trigger,
        )
    else:
        result = _pull_via_service(
            cs,
            overrides,
            max_sessions=max_sessions,
            user_alias=user_alias,
            force=force,
            defer_trigger=defer_trigger,
        )

    counts = result.get("counts") or {}
    click.echo(
        f"Pulled {result.get('total', 0)} session(s): "
        f"queued={counts.get('queued', 0)}, "
        f"skipped={counts.get('skipped', 0)}, "
        f"duplicate={counts.get('duplicate', 0)}, "
        f"empty={counts.get('empty', 0)}, "
        f"error={counts.get('error', 0)}"
    )
    for item in result.get("results") or []:
        click.echo(f"  {item.get('session_id', '?')}: {item.get('status', '?')}")


def _pull_in_process(
    cfg,
    overrides: dict,
    *,
    max_sessions: int,
    user_alias: str,
    force: bool,
    defer_trigger: bool,
) -> dict:
    import asyncio

    from ..integrations.langfuse_client import LangfuseError
    from ..integrations.langfuse_pull import pull_sessions
    from ..session_store import SessionStore

    try:
        store = SessionStore.from_config(cfg)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"session storage is not configured for in-process ingest: {exc}"
        ) from None

    from ..session_filter import SessionValueClassifier

    classifier = SessionValueClassifier.from_config(cfg)

    async def _ingest(session: dict) -> dict:
        if not force and store.duplicate_of_processed(session):
            return {"status": "duplicate", "session_id": session.get("session_id"), "queued": False}
        judge = await classifier.classify(session)
        session["value_judge"] = judge
        if judge.get("decision") != "valuable":
            store.save_skipped(session)
            return {"status": "skipped", "queued": False, "value_judge": judge}
        store.save_queued(session)
        return {"status": "queued", "queued": True, "value_judge": judge}

    try:
        return asyncio.run(
            pull_sessions(
                cfg,
                _ingest,
                overrides,
                max_sessions=max_sessions,
                user_alias=user_alias,
                force_reprocess=force,
                defer_evolution_trigger=defer_trigger,
            )
        )
    except LangfuseError as exc:
        raise click.ClickException(str(exc)) from None


def _pull_via_service(
    cs: ConfigStore,
    overrides: dict,
    *,
    max_sessions: int,
    user_alias: str,
    force: bool,
    defer_trigger: bool,
) -> dict:
    import os
    import urllib.error
    import urllib.request

    base_url = _service_base_url(cs)
    payload = dict(overrides)
    payload["max_sessions"] = max_sessions
    if user_alias:
        payload["user_alias"] = user_alias
    if force:
        payload["force_reprocess"] = True
    if defer_trigger:
        payload["defer_evolution_trigger"] = True

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/langfuse/pull", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    api_key = str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise click.ClickException(
            f"service pull failed (HTTP {exc.code}): {detail}"
        ) from None
    except urllib.error.URLError as exc:
        raise click.ClickException(
            f"cannot reach teamEvolver service at {base_url}: {exc.reason}. "
            "Start it with 'teamEvolver start', or use --in-process."
        ) from None
