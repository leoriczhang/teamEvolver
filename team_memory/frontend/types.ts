import type { SkillLabChecklistReport } from "@/api/client";

export interface MemoryReplayBranch {
  ok?: boolean;
  error?: string;
  final_response?: string;
  trajectory?: string;
  interaction_turns?: number;
  tool_call_count?: number;
  total_tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  context_input_hash?: string;
  checklist_report?: SkillLabChecklistReport;
  safety_report?: Record<string, any>;
}

export interface MemoryTrueReplay {
  replay_id: string;
  change_id: string;
  status?: "evaluated" | "failed";
  runtime?: string;
  source_session_id?: string;
  query?: string;
  checklist?: Array<{ id?: string; text?: string }>;
  verdict?: "accept" | "reject" | "inconclusive";
  accepted?: boolean;
  no_regression?: boolean;
  reason?: string;
  completed_at?: string;
  treatment?: {
    before_oid?: string;
    after_oid?: string;
    before_hash?: string;
    after_hash?: string;
    action?: string;
  };
  efficiency?: {
    dimensions?: Record<string, {
      baseline: number;
      candidate: number;
      delta: number;
      reduction_ratio: number;
      winner: "candidate" | "baseline" | "tie";
    }>;
  };
  cases?: Array<{
    baseline?: MemoryReplayBranch;
    candidate?: MemoryReplayBranch;
  }>;
}

export interface MemoryChangeRecord {
  change_id: string;
  run_id?: string;
  job_name?: string;
  action?: string;
  result?: string;
  snapshot_status?: string;
  risk_level?: string;
  before_oid?: string;
  after_oid?: string;
  before_hash?: string;
  after_hash?: string;
  before_exists?: boolean;
  after_exists?: boolean;
  target_paths?: string[];
  policy_reasons?: string[];
  completed_at?: string;
  latest_replay?: MemoryTrueReplay;
}

export interface MemoryChangeList {
  schema_version: string;
  count: number;
  items: MemoryChangeRecord[];
}

export interface MemoryTrueReplayList {
  schema_version: string;
  change_id: string;
  count: number;
  items: MemoryTrueReplay[];
}


export interface AggregationSettings {
  enabled: boolean;
  shared_knowledge_prefix: string;
  target_root: string;
  staging_dir: string;
  work_root: string;
  okf_skill_uri: string;
  maintenance_skill_uri: string;
  key_seed: string;
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

export interface AggregationSettingsUpdate {
  shared_knowledge_prefix?: string;
  staging_dir?: string;
  okf_skill_uri?: string;
  maintenance_skill_uri?: string;
  kinds?: string[];
  account_user_limit?: number;
  account_user_page_size?: number;
  phase1_concurrency?: number;
  merge_fan_in?: number;
  merge_concurrency?: number;
  partition_threshold?: number;
  partition_count?: number;
  run_detail_limit?: number;
}

