"""Customer deployment entry point without optional Agent runtimes."""

from __future__ import annotations

import os
from pathlib import Path


def create_app():
    from .config_store import ConfigStore
    from .proxy.server import ProxyServer

    store = ConfigStore()
    if not store.exists() and os.environ.get("TEAMEVOLVER_CONFIG_BOOTSTRAP") == "1":
        template = Path(__file__).resolve().parents[1] / "docker/customer.yaml"
        store.save(ConfigStore(template).load())
    if not store.exists():
        raise RuntimeError("Set TEAMEVOLVER_CONFIG_FILE to the customer YAML configuration")
    config = store.to_config()
    if not config.storage_pg_enabled:
        raise RuntimeError("Customer deployment requires storage_pg.enabled")
    if len(os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")) < 32:
        raise RuntimeError("TEAMEVOLVER_ROOT_API_KEY must contain at least 32 characters")
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise RuntimeError("Use one ASGI worker; horizontal console session coordination is not enabled")
    os.environ.setdefault("TEAMEVOLVER_SKILLMINER_ENABLED", "1")
    os.environ.setdefault("TEAMEVOLVER_LLM_MAX_OUTPUT_TOKENS", "8192")
    os.environ["TEAMEVOLVER_CUSTOMER_MODE"] = "1"
    server = ProxyServer(config)
    return server.app
