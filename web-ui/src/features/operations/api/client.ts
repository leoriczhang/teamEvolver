

export interface StorageStatus {
  backend?: string;
  deployment?: string;
  endpoint?: string;
  namespace?: string;
  api_key_present?: boolean;
  reachable?: boolean;
  reason?: string;
  fallback_enabled?: boolean;
  effective_backend?: string;
  fallback_active?: boolean;
  local_root?: string;
  session_backend?: string;
  skill_backend?: string;
  mirror_enabled?: boolean;
  mirror?: {
    enabled?: boolean;
    spool_dir?: string;
    backlog?: number;
    oldest_age_seconds?: number;
    dead_letter?: number;
    last_error?: string;
    error?: string;
  };
  // Postgres-backed storage pool health (present only when a PG
  // backend is configured for the selected tenant).
  pg?: {
    reachable?: boolean;
    ping_ms?: number | null;
    pool_size?: number;
    pool_idle?: number;
    pool_min?: number;
    pool_max?: number;
    tenant_id?: string;
  };
  // Present on the sharing-config save response: OpenViking directory skeleton
  // check/create result for the just-saved connection.
  openviking_dirs?: VikingDirsReport;
}

export type VikingDeployment = "cloud" | "local";

export interface VikingDirsReport {
  action?: "ok" | "partial" | "failed";
  account_id?: string;
  endpoint?: string;
  checked?: number;
  created?: string[];
  existing?: string[];
  errors?: Array<{ uri: string; error: string }>;
  error?: string;
}

export interface SharingConfig {
  deployment?: VikingDeployment;
  endpoint?: string;
  endpoint_override?: string;
  cloud_endpoint?: string;
  local_endpoint?: string;
  account?: string;
  team_user?: string;
  root_prefix?: string;
  api_key_present?: boolean;
}

export interface TeamSettings {
  display_name: string;
  configured_display_name: string;
  environment_override?: string;
  override_source?: string;
}

export interface LangfuseTracingStatus {
  enabled: boolean;
  sdk_available?: boolean;
  initialized?: boolean;
  host?: string;
  environment?: string;
  release?: string;
  sample_rate?: number;
  capture_content?: boolean;
  last_error?: string;
}

export interface LangfuseTracingConfig {
  enabled: boolean;
  host: string;
  public_key_present?: boolean;
  secret_key_present?: boolean;
  environment?: string;
  release?: string;
  sample_rate?: number;
  capture_content?: boolean;
  flush_at?: number;
  flush_interval_seconds?: number;
  status?: LangfuseTracingStatus;
  // Write-only fields; the server returns presence flags instead.
  public_key?: string;
  secret_key?: string;
  clear_public_key?: boolean;
  clear_secret_key?: boolean;
}
