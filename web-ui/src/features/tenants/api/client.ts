import { api } from "@/api/client";

export interface TenantInfo {
  tenant_id: string;
  display_name: string;
  status: string; // active | disabled
}

export interface TenantsResp {
  mode: "postgres" | "single";
  /** Single-tenant deployments configure one credential instead of issuing per-tenant ones. */
  machine_credential?: {
    configured: boolean;
    env_var: string;
    prefix: string;
  };
  tenants: TenantInfo[];
}

export interface OpenvikingAccountBinding {
  account_id: string;
  action: "created" | "bound" | "failed";
  error?: string;
}

export interface CreateTenantResp {
  tenant: TenantInfo;
  // Tenant administrators can retrieve the retained agent token later.
  agent_token: string;
  openviking_account: OpenvikingAccountBinding;
}

export async function listTenants(): Promise<TenantsResp> {
  return api<TenantsResp>("/api/tenants");
}

export async function createTenant(displayName: string, accountId = ""): Promise<CreateTenantResp> {
  return api<CreateTenantResp>("/api/tenants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, account_id: accountId }),
  });
}

export async function rotateTenantToken(tenantId: string): Promise<{ agent_token: string }> {
  return api<{ agent_token: string }>(`/api/tenants/${encodeURIComponent(tenantId)}/rotate-token`, {
    method: "POST",
  });
}

export interface TenantTokenResp {
  tenant_id: string;
  agent_token: string | null;
  available: boolean;
}

export async function getTenantToken(tenantId: string): Promise<TenantTokenResp> {
  return api<TenantTokenResp>(`/api/tenants/${encodeURIComponent(tenantId)}/token`, { cache: "no-store" });
}

export async function importTenantToken(tenantId: string, token: string): Promise<TenantTokenResp> {
  return api<TenantTokenResp>(`/api/tenants/${encodeURIComponent(tenantId)}/token`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_token: token }),
  });
}

export async function setTenantStatus(
  tenantId: string,
  status: "active" | "disabled",
): Promise<{ tenant_id: string; status: string }> {
  return api<{ tenant_id: string; status: string }>(
    `/api/tenants/${encodeURIComponent(tenantId)}/status`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
}

export interface TenantConfigResp {
  tenant_id: string;
  display_name: string;
  // Flat TeamEvolverConfig-field-name overrides stored in tenants.config.
  config_overrides: Record<string, unknown>;
  secret_presence?: Record<string, boolean>;
  // Every key the server accepts in a config update (field names + quotas).
  editable_keys: string[];
}

export async function getTenantConfig(tenantId: string): Promise<TenantConfigResp> {
  return api<TenantConfigResp>(`/api/tenants/${encodeURIComponent(tenantId)}/config`);
}

export async function updateTenantConfig(
  tenantId: string,
  overrides: Record<string, unknown>,
): Promise<{ tenant_id: string; config_overrides: Record<string, unknown>; secret_presence?: Record<string, boolean> }> {
  return api<{ tenant_id: string; config_overrides: Record<string, unknown>; secret_presence?: Record<string, boolean> }>(
    `/api/tenants/${encodeURIComponent(tenantId)}/config`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides }),
    },
  );
}

export interface TenantRuntimeConfig {
  collection_concurrency: number;
  ingestion_concurrency: number;
  analysis_concurrency: number;
  evolution_concurrency: number;
  replay_concurrency: number;
  background_concurrency: number;
  llm_max_concurrency: number;
  llm_queue_capacity: number;
}

export interface TenantRuntimeResp {
  tenant_id: string;
  revision: number;
  config: TenantRuntimeConfig;
}

export function getTenantRuntimeConfig(tenantId: string) {
  return api<TenantRuntimeResp>(`/api/tenants/${encodeURIComponent(tenantId)}/runtime-config`);
}

export function updateTenantRuntimeConfig(tenantId: string, current: TenantRuntimeResp) {
  return api<TenantRuntimeResp>(`/api/tenants/${encodeURIComponent(tenantId)}/runtime-config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: current.revision, config: current.config }),
  });
}
