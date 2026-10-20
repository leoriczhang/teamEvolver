export interface AggregationSettings {
  revision: number;
  enabled: boolean;
  maintenance_trigger_mode: "manual" | "automatic";
  maintenance_interval_seconds: number;
  shared_knowledge_prefix: string;
  target_root: string;
  staging_dir: string;
  work_root: string;
  okf_skill_uri: string;
  maintenance_skill_uri: string;
  kinds: string[];
  account_user_limit: number;
  account_user_page_size: number;
  phase1_concurrency: number;
  merge_fan_in: number;
  merge_concurrency: number;
  partition_threshold: number;
  partition_count: number;
  run_detail_limit: number;
}
