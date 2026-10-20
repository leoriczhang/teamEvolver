import type { TenantInfo } from "@/features/tenants/api/client";

export type SkillSpaceBackend = "" | "viking";

export interface SkillSpaceConfig {
  backend?: SkillSpaceBackend;
  viking_user?: string;
  bound?: boolean;
}

export interface OpenVikingAccountsResp {
  accounts: string[];
  current: string;
  source?: "openviking" | "fallback";
  endpoint?: string;
  error?: string;
}

export interface OpenVikingAccountUser {
  user_id: string;
  role: "member" | "tenant_admin";
  openviking_role?: string;
  imported?: boolean;
}

export interface OpenVikingAccountUsersResp {
  account: string;
  users: OpenVikingAccountUser[];
  source?: "openviking" | "fallback";
  endpoint?: string;
  error?: string;
}

export interface ImportAccountUsersResp {
  account: string;
  imported: string[];
  skipped_existing: string[];
  missing: string[];
}

export interface UserProfile {
  id: string;
  display_name?: string;
  email?: string;
  role?: "user" | "admin";
  platform_role?: "user" | "platform_admin";
  tenant_role?: "member" | "tenant_admin" | null;
  tenant_id?: string | null;
  status?: "active" | "disabled";
  capabilities?: string[];
  password?: string;
  password_set?: boolean;
  personal_space?: SkillSpaceConfig;
  team_space?: SkillSpaceConfig;
  created_at?: string;
  updated_at?: string;
}

export interface UsersListResp {
  storage_deployment?: string;
  openviking_account?: string;
  default_team_user?: string;
  users: UserProfile[];
}

export interface AuthStatus {
  customer_mode?: boolean;
  self_registration_enabled?: boolean;
  authenticated: boolean;
  needs_setup?: boolean;
  user?: UserProfile | null;
  available_tenants?: TenantInfo[];
  active_tenant_id?: string | null;
}
