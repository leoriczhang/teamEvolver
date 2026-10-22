"""Application and Worker composition root; every endpoint is owned by a module API."""

from __future__ import annotations

from .runtime.http_runtime import install_http_runtime

# Platform module routers composed into the single app. Each entry is imported
# lazily inside ``create_app`` and mounted via its ``create_router`` factory;
# the composition root is the only place these business/platform api packages
# are imported.
_PLATFORM_API_MODULES = (
    "teamEvolver.observability.api",
    "teamEvolver.operations.api",
    "teamEvolver.admin.api",
    "teamEvolver.tenants.api",
    "teamEvolver.platform_docs.api",
    "teamEvolver.context.api",
    "session_ingestion.api",
    "team_skills.api",
    "team_replay.api",
    "team_memory.api",
    "team_miner.api",
)


def create_app(
    *,
    services=None,
    lifecycle=None,
    tenant_resolver=None,
    authorizer=None,
    title: str = "teamEvolver",
):
    """Create the single FastAPI application and assemble all module routers.

    ``services`` is the runtime :class:`RuntimeServices` container; ``lifecycle``
    (optional) drives readiness and graceful drain. ``tenant_resolver`` /
    ``authorizer`` are injected into the HTTP runtime so this module stays free
    of identity logic. Static assets are mounted when present.
    """
    import importlib
    import os

    from fastapi import FastAPI

    app = FastAPI(title=title)

    # 1. common middleware (request_id/trace_id, TenantContext set+reset, RBAC
    #    hook, exception mapping, access log). Platform middleware.
    #    When no explicit authorizer is supplied but the services container
    #    carries the console session manager + config (api role), build the
    #    console authorizer + cookie-renewal response hook here so identity logic
    #    stays in teamEvolver/admin, not in this composition root.
    response_hook = None
    if authorizer is None:
        authorizer, response_hook = _build_console_identity(services)
    install_http_runtime(
        app,
        services,
        tenant_resolver=tenant_resolver,
        authorizer=authorizer,
        response_hook=response_hook,
    )

    _wire_domain_contracts(services)

    # 2. assemble each module router through its create_router factory. The
    #    composition root is the only importer of these packages.
    seen: dict[tuple[str, str], str] = {}
    for module_path in _PLATFORM_API_MODULES:
        module = importlib.import_module(module_path)
        router = module.create_router(services, lifecycle=lifecycle)
        _reject_duplicate_routes(router, module_path, seen)
        app.include_router(router)

    # 3. static assets: SPA console bundle + docs assets, mounted only if built.
    _mount_static(app, os)

    app.state.services = services
    app.state.lifecycle = lifecycle
    return app


def _wire_domain_contracts(services):
    """Inject cross-domain contracts; modules never import each other's handlers."""
    if services is None or not services.extras.get("config_repo"):
        return
    from team_memory.changes import read_change
    from team_replay.commands import enqueue
    from team_replay.config import run_options
    from team_replay.handlers import run as replay_handler
    from team_replay.repository import ReplayRepository
    from team_skills.library.frontmatter import parse_skill_md_text

    from .context.usage import verify_usage
    from .tenants.registry import current_tenant_id

    services.extras.setdefault("context_usage_verifier", lambda p, turns: verify_usage(services, p, turns))
    services.extras.setdefault("skill_metadata_reader", parse_skill_md_text)
    from .openviking.settings import effective as openviking_config
    async def compile_client(payload, user=None):
        from .openviking import OpenVikingClient
        cfg = await openviking_config(services, current_tenant_id())
        return OpenVikingClient(endpoint=cfg.sharing_viking_endpoint, api_key=cfg.sharing_viking_api_key,
                                 account_id=payload["account_id"], user=user or payload["user_id"],
                                 deployment=cfg.sharing_viking_deployment)
    services.extras.setdefault("compile_client_factory", compile_client)

    async def accept_replay(spec, raw, tenant):
        async with services.tx.tenant_tx(tenant) as conn:
            record = await enqueue(conn, services, tenant_id=tenant, domain="memory",
                                   run_id=spec.run_id, subject_id=spec.subject_id, request=raw, spec=spec,
                                   options=run_options({k: raw[k] for k in
                                                        ("timeout_seconds", "max_interactions") if k in raw}))
            await conn.execute(
                f"UPDATE {services.tx.pool.schema}.worker_tasks SET kind='memory.replay' WHERE task_id=$1",
                spec.run_id,
            )
        return record

    async def list_replays(tenant, change_id, limit):
        async with services.tx.tenant_tx(tenant) as conn:
            return await ReplayRepository(services.tx).list(conn, "memory", subject_id=change_id, limit=limit)

    services.extras.setdefault("memory_replay_accept", accept_replay)
    services.extras.setdefault("memory_replay_list", list_replays)
    services.extras.setdefault("memory_replay_handler", replay_handler)
    services.extras.setdefault("memory_change_reader",
                               lambda change: read_change(services, current_tenant_id(), change))


def build_role_services(*, role, tx, config, handlers=None):
    """Assemble domain handlers only for Worker roles at the composition root."""
    from session_ingestion.payloads import SessionPayloads

    from .config_runtime import ConfigRepository
    from .runtime.services import build_services
    extras = {"config": config, "config_repo": ConfigRepository(tx), "session_payloads": SessionPayloads(tx)}
    if role in ("api", "all"):
        from .admin.console_session import ConsoleSessionManager
        extras["console_sessions"] = ConsoleSessionManager(config)
    domain_handlers = {}
    if role in ("worker", "all"):
        from session_ingestion.handlers import register_handlers as ingestion_handlers
        from team_memory.handlers import register_handlers as memory_handlers
        from team_miner.handlers import register_handlers as miner_handlers
        from team_replay.handlers import register_handlers as replay_handlers
        from team_skills.handlers import register_handlers as skill_handlers
        for factory in (ingestion_handlers, skill_handlers, replay_handlers, memory_handlers, miner_handlers):
            for kind, handler in factory().items():
                if kind in domain_handlers:
                    raise RuntimeError(f"duplicate Worker Handler: {kind}")
                domain_handlers[kind] = handler
        if handlers is not None:
            domain_handlers = handlers
        else:
            from .runtime.tasks import build_task_registry
            build_task_registry(domain_handlers, require_complete=True)
    secrets = _secret_repository(tx)
    if secrets is not None:
        extras["secret_repo"] = secrets
    services = build_services(role=role, tx=tx, handlers=domain_handlers, extras=extras)
    if services.runs_worker:
        from session_ingestion.adapters.worker import AdapterSource
        from session_ingestion.handlers import SessionIngestionDeps
        from session_ingestion.split import split_session
        from session_ingestion.stages import SessionStages
        from team_skills.evolution.cycle import LLMSkillPlanner
        from team_skills.evolution.model import model_for
        from team_skills.evolution.prompt_context import prompt_context
        from team_skills.evolution.replay_case_synthesis import LLMCrossSessionCaseSynthesizer
        from team_skills.evolution.stages.analyze import analyze_session
        from team_skills.evolution.stages.trace_analyze import analyze_session_traces

        from .tenants.registry import current_tenant_id

        async def analyze(s):
            tenant = current_tenant_id()
            async with tx.tenant_tx(tenant) as conn:
                row = await extras["config_repo"].read_tenant_module(conn, "skills")
            with prompt_context(row.config):
                client = await model_for(services, tenant, row.config.get("model", {}))
                return await analyze_session(client, s)

        async def split(s):
            async def client():
                return await model_for(services, current_tenant_id())

            return await split_session(config, s, client_factory=client)

        async def trace(s):
            client = await model_for(services, current_tenant_id())
            return await analyze_session_traces(client, s)

        stages = SessionStages(extras["session_payloads"], split, analyze, trace)
        services.extras["session_ingestion"] = SessionIngestionDeps(
            payloads=extras["session_payloads"], stage_runner=stages, adapter_source=AdapterSource())
        async def planner(**kwargs):
            client = await model_for(services, current_tenant_id(), kwargs["config"].config.get("model", {}))
            return await LLMSkillPlanner(client)(**kwargs)
        services.extras["skill_planner"] = planner
        async def replay_case_synthesizer(**kwargs):
            client = await model_for(services, current_tenant_id())
            return await LLMCrossSessionCaseSynthesizer(client)(**kwargs)
        services.extras["replay_case_synthesizer"] = replay_case_synthesizer
        from team_skills.experiences.patterns import PatternCompiler
        async def pattern_compiler(**kwargs):
            return await PatternCompiler(await model_for(services, current_tenant_id()))(**kwargs)
        services.extras["experience_pattern_compiler"] = pattern_compiler
        services.extras["scheduled_tasks"] = _scheduled_tasks(services)
    _wire_domain_contracts(services)
    return services


def _secret_repository(tx):
    """Encrypted Secret Ref store, available when a master key is configured."""
    import os

    master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
    if not master:
        return None
    from .security import SecretBox, SecretRepository
    return SecretRepository(tx, SecretBox(master))


def _scheduled_tasks(services):
    """Per-tenant dispatch passes the Worker loop runs on each tenant refresh.

    Each pass is bounded and lease-guarded, so every Worker replica may run it:
    datasource pull, Skill evolution, Memory maintenance and experience
    dispatchers. One failing pass does not skip the others.
    """
    import logging

    from session_ingestion.schedule import enqueue_due
    from team_memory.schedule import memory_tick
    from team_skills.evolution.schedule import evolution_tick
    from team_skills.experiences.supervisor import experience_tick

    logger = logging.getLogger(__name__)
    passes = (
        ("datasource_schedule", lambda tenant: enqueue_due(services, tenant)),
        ("skill_evolution", lambda tenant: evolution_tick(services, tenant_id=tenant)),
        ("memory_maintenance", lambda tenant: memory_tick(services, tenant_id=tenant)),
        ("experience_sync", lambda tenant: experience_tick(services, tenant_id=tenant)),
    )
    tracing = _tracing_refresh(services)

    async def run(tenant):
        try:
            await tracing()
        except Exception:
            logger.exception("worker.dispatch_error", extra={"tenant": tenant, "dispatch": "tracing_config"})
        for name, dispatch in passes:
            try:
                await dispatch(tenant)
            except Exception:
                logger.exception("worker.dispatch_error", extra={"tenant": tenant, "dispatch": name})

    return run


_TRACING_FIELDS = ("enabled", "host", "environment", "release", "sample_rate", "capture_content",
                   "flush_at", "flush_interval_seconds", "public_key")


def _tracing_refresh(services, *, min_interval=10.0):
    """Apply the global ``tracing`` config row over bootstrap values to the Langfuse runtime.

    The console saves outbound tracing settings to ``teamevolver_config``; the
    Worker, which issues the traced model calls, re-reads them on its refresh
    cadence. ``LANGFUSE_*`` environment variables still take precedence.
    """
    import time
    from types import SimpleNamespace

    from .observability import configure_langfuse

    config = services.extras.get("config")
    repo = services.extras.get("config_repo")
    state = {"at": None}

    async def refresh():
        now = time.monotonic()
        if repo is None or (state["at"] is not None and now - state["at"] < min_interval):
            return
        state["at"] = now
        async with services.tx.pool.admin_conn() as conn:
            raw = dict((await repo.read_global(conn)).config.get("tracing", {}))
        secret = getattr(config, "langfuse_tracing_secret_key", "")
        secrets = services.extras.get("secret_repo")
        if raw.get("secret_ref") and secrets is not None:
            async with services.tx.tenant_tx("default") as conn:
                secret = await secrets.resolve(conn, secret_ref=raw["secret_ref"])
        values = {name: getattr(config, name) for name in dir(config) if name.startswith("langfuse_tracing_")}
        values.update({f"langfuse_tracing_{k}": raw[k] for k in _TRACING_FIELDS if k in raw})
        values["langfuse_tracing_secret_key"] = secret
        configure_langfuse(SimpleNamespace(**values))

    return refresh


def _build_console_identity(services):
    """Build the console authorizer + cookie-renewal hook from ``services``.

    Returns ``(authorizer, response_hook)`` when the services container carries
    the console session manager and config in ``extras`` (api role); otherwise
    ``(None, None)`` so a minimal/worker app installs no identity logic. Keeping
    this wiring here (and the logic in ``teamEvolver/admin``) means the HTTP
    runtime stays business-free.

    The authorizer resolves the full tenant identity (console ``X-Tenant-Id``
    selector + ``tevt_`` Agent token) via the process-wide ``TenantRegistry``.
    ``extras['tenant_registry']`` may inject a prebuilt/fake registry (tests,
    single shared control pool); otherwise one is lazily built from ``config``.
    """
    extras = getattr(services, "extras", None) or {}
    sessions = extras.get("console_sessions")
    config = extras.get("config")
    if sessions is None or config is None:
        return None, None

    from .admin.authn import build_console_authorizer, set_console_cookie

    registry_provider = _tenant_registry_provider(extras, config)
    authorizer = build_console_authorizer(
        sessions, config=config, registry_provider=registry_provider,
    )

    # Endpoints that issue/clear their own cookie must not be overwritten by the
    # renewal hook with the previous session .
    _COOKIE_OWNED = {
        "/api/auth/login", "/api/auth/register", "/api/auth/bootstrap", "/api/auth/logout",
    }

    def response_hook(request, response):
        token = (
            getattr(request.state, "renew_console_session", "")
            or getattr(request.state, "migrate_console_session", "")
        )
        if token and request.url.path not in _COOKIE_OWNED:
            set_console_cookie(response, request, token)

    return authorizer, response_hook


def _tenant_registry_provider(extras, config):
    """Return a zero-arg provider yielding the process-wide ``TenantRegistry``.

    Prefers an injected ``extras['tenant_registry']`` (tests / a shared control
    pool); otherwise lazily builds (and memoises) a ``TenantRegistry(config)``
    so a PG control pool is only created on first use.
    """
    injected = extras.get("tenant_registry")
    if injected is not None:
        return lambda: injected

    cache: dict[str, object] = {}

    def provider():
        registry = cache.get("registry")
        if registry is None:
            from .tenants.registry import TenantRegistry

            registry = TenantRegistry(config)
            cache["registry"] = registry
        return registry

    return provider


def _reject_duplicate_routes(router, module_path: str, seen: dict[tuple[str, str], str]) -> None:
    """Fail startup when two modules declare the same method and path."""
    for route in router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None:
            continue
        for method in methods:
            key = (method, path)
            if key in seen:
                raise RuntimeError(
                    f"application composition: duplicate route {method} {path} "
                    f"declared by both {seen[key]} and {module_path}"
                )
            seen[key] = module_path


def _mount_static(app, os) -> None:
    from fastapi.staticfiles import StaticFiles

    here = os.path.dirname(__file__)
    dist_assets = os.path.abspath(os.path.join(here, "web", "dist", "assets"))
    if os.path.isdir(dist_assets):
        app.mount("/assets", StaticFiles(directory=dist_assets), name="assets")
    docs_assets = os.path.abspath(os.path.join(here, "..", "docs", "assets"))
    if os.path.isdir(docs_assets):
        app.mount("/docs-assets", StaticFiles(directory=docs_assets), name="docs-assets")
