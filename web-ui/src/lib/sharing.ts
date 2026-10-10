import type { SharingConfig } from "@/api/client";

/** OpenViking 是否已具备可用连接配置。 */
export function hasOpenVikingConfiguration(config?: SharingConfig | null): boolean {
  return Boolean(
    config?.endpoint?.trim()
    && config.api_key_present,
  );
}
