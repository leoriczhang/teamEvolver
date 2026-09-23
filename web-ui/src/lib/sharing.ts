import type { SharingConfig } from "@/api/client";

/** OpenViking 是否已具备可用配置（后端 enabled + endpoint + 任一 Key）。 */
export function hasOpenVikingConfiguration(config?: SharingConfig | null): boolean {
  return Boolean(
    config?.enabled
    && config.endpoint?.trim()
    && (config.service_api_key_present || config.team_api_key_present),
  );
}