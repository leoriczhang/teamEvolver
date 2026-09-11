"""Tenant management (multi-tenancy plan Phase 1)."""

from .registry import (
    AGENT_TOKEN_PREFIX,
    DEFAULT_TENANT_ID,
    TenantContext,
    TenantRegistry,
    apply_tenant_config_overrides,
    current_tenant_id,
    effective_config,
    generate_agent_token,
    get_current_tenant,
    hash_agent_token,
    reset_current_tenant,
    set_current_tenant,
)

__all__ = [
    "AGENT_TOKEN_PREFIX",
    "DEFAULT_TENANT_ID",
    "TenantContext",
    "TenantRegistry",
    "apply_tenant_config_overrides",
    "current_tenant_id",
    "effective_config",
    "generate_agent_token",
    "get_current_tenant",
    "hash_agent_token",
    "reset_current_tenant",
    "set_current_tenant",
]
