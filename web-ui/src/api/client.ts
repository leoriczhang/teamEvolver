// API client for the teamEvolver unified console.
//
// All requests are same-origin: the 52010 evolve server hosts this SPA and
// serves dashboard, auth, user, skill, model-config and session-ingest endpoints natively.
// In dev, vite.config.ts proxies these paths to 127.0.0.1:52010.

export class ApiError extends Error {}

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(path, opts);
  const text = await res.text();
  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new ApiError(
      data?.detail || data?.msg || data?.raw || res.statusText || `${path} -> ${res.status}`
    );
  }
  return data as T;
}

// ---- Dashboard (evolve server, native on 52010) --------------------------- //

export interface StatusResp {
  running: boolean;
  pending_sessions: number;
  registered_skills: number;
  skills: Record<string, { skill_id?: string; version?: number }>;
}

export interface StorageStatus {
  backend?: string;
  deployment?: string;
  endpoint?: string;
  namespace?: string;
  api_key_present?: boolean;
  reachable?: boolean;
  reason?: string;
}

export type VikingDeployment = "cloud" | "local";

export interface SharingConfig {
  enabled?: boolean;
  backend?: string;
  deployment?: VikingDeployment;
  endpoint?: string;
  endpoint_override?: string;
  cloud_endpoint?: string;
  local_endpoint?: string;
  account?: string;
  personal_user?: string;
  team_user?: string;
  root_prefix?: string;
  team_api_key_present?: boolean;
  personal_api_key_present?: boolean;
}

export interface TeamSettings {
  display_name: string;
  configured_display_name: string;
  environment_override?: string;
  override_source?: string;
}

export interface QueueSession {
  user_alias?: string;
  session_id: string;
  num_turns?: number;
  timestamp?: string;
}

export interface PageResponse<T> {
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  sessions?: T[];
  conversations?: T[];
  candidates?: T[];
}

export interface LedgerRow {
  session_id: string;
  title?: string;
  user_alias?: string;
  num_turns?: number | null;
  status?: string;
  consumed_at?: string;
  ingested_at?: string;
  timestamp?: string;
}

export interface BundleFileDiff {
  path: string;
  status: "added" | "modified" | "deleted" | "unchanged";
  old_sha256?: string;
  new_sha256?: string;
  old_size?: number;
  new_size?: number;
  is_text?: boolean;
  diff?: string;
}

export interface BundleDiff {
  before_tree_sha256?: string;
  after_tree_sha256?: string;
  changed_count?: number;
  files?: BundleFileDiff[];
}

export interface StaticValidation {
  passed?: boolean;
  enabled?: boolean;
  changed_files?: string[];
  errors?: string[];
  checks?: Array<{
    path?: string;
    checker?: string;
    passed?: boolean;
    detail?: string;
  }>;
}

export interface Candidate {
  job_id: string;
  skill_name?: string;
  candidate_skill_name?: string;
  proposed_action?: string;
  review_status?: string;
  rationale?: string;
  evidence_classification?: EvidenceClassification;
  content_preview?: string;
  source?: {
    kind?: string;
    skill_name?: string;
    artifact_sha256?: string;
    dataset_format?: string;
    question_count?: number;
    submitted_by?: string;
  };
  candidate_skill?: {
    name?: string;
    description?: string;
    category?: string;
    content?: string;
    edit_summary?: Record<string, any>;
    file_changes?: Array<{
      path?: string;
      operation?: "upsert" | "delete";
      reason?: string;
    }>;
    static_validation?: StaticValidation;
  };
  current_skill?: {
    name?: string;
    description?: string;
    category?: string;
    content?: string;
  } | null;
  current_skill_md?: string;
  candidate_skill_md?: string;
  skill_diff?: string;
  bundle_diff?: BundleDiff;
  static_validation?: StaticValidation;
  recommended_publish?: boolean;
  evaluation_error?: string | null;
  test_dataset_count?: number;
  test_dataset_ids?: string[];
  evaluation?: EvalResult;
  decision?: CandidateDecision;
  decision_reason?: string;
  decided_at?: string;
  decision_accepted?: boolean | null;
}

export interface CandidateDecision {
  status?: string;
  accepted?: boolean;
  reason?: string;
  decided_at?: string;
  job_id?: string;
  skill_name?: string;
  version?: number;
  evaluation?: EvalResult;
  [key: string]: any;
}

export interface EvalResult {
  skill_name?: string;
  proposed_action?: string;
  recommended_publish?: boolean;
  cached?: boolean;
  replay?: {
    verdict?: "accept" | "reject" | "inconclusive";
    no_regression?: boolean;
    error?: string;
    cases?: ReplayCase[];
    efficiency?: {
      improved_dimensions?: string[];
      regressed_dimensions?: string[];
      unchanged_dimensions?: string[];
      dimensions?: Record<string, {
        baseline: number;
        candidate: number;
        delta: number;
        reduction_ratio: number;
        winner: "candidate" | "baseline" | "tie";
      }>;
    };
    decision_policy?: ReplayDecisionPolicy;
    checklist?: {
      baseline?: SkillLabChecklistReport;
      candidate?: SkillLabChecklistReport;
    };
  };
  candidate_skill?: Candidate["candidate_skill"];
  current_skill?: Candidate["current_skill"];
  current_skill_md?: string;
  candidate_skill_md?: string;
  skill_diff?: string;
  bundle_diff?: BundleDiff;
  static_validation?: StaticValidation;
}

export interface ReplayDecisionPolicy {
  accepted?: boolean;
  policy?: string;
  verdict?: "accept" | "reject" | "inconclusive";
  decision_basis?: string;
  primary_metric?: string;
  secondary_metrics?: string[];
  decisive_metrics?: string[];
  no_regression?: boolean;
  metric_changes?: Record<string, {
    baseline?: number;
    candidate?: number;
    delta?: number;
    status?: "improved" | "regressed" | "unchanged";
  }>;
  improved_metrics?: string[];
  regressed_metrics?: string[];
  unchanged_metrics?: string[];
  all_windows_evaluated?: boolean;
}

export interface ReplaySide {
  response?: string;
  final_response?: string;
  response_text?: string;
  error?: string;
  rationale?: string;
  instruction?: string;
  session_id?: string;
  turn_num?: number | null;
  interaction_turns?: number | null;
  tool_call_count?: number | null;
  total_tokens?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  interactions?: Array<Record<string, any>>;
  checklist_report?: SkillLabChecklistReport;
}
export interface ReplayCase {
  baseline?: ReplaySide;
  candidate?: ReplaySide;
}

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

export interface SkillVersionResp {
  skill_id?: string;
  category?: string;
  description?: string;
  content?: string;
  raw_md?: string;
  version: number;
  current_version: number;
  is_current?: boolean;
  versions?: number[];
  tree_sha256?: string;
  files?: Array<{ path: string; sha256?: string; size?: number }>;
  evolution?: {
    job_id?: string;
    proposed_action?: string;
    rationale?: string;
    edit_summary?: Record<string, any>;
    optimization_items?: string[];
    evidence_classification?: EvidenceClassification;
    decision?: CandidateDecision;
    evaluation?: EvalResult;
    skill_diff?: string;
    bundle_diff?: BundleDiff;
    static_validation?: StaticValidation;
  };
}

export interface SessionDetail {
  meta?: {
    title?: string;
    user_alias?: string;
    status?: string;
    num_turns?: number | null;
  };
  turns_available?: boolean;
  turns_source?: string;
  system_prompt?: string;
  injected_skills?: string[];
  used_skills?: string[];
  metrics?: {
    interaction_turns?: number;
    message_count?: number;
    tool_call_count?: number;
    api_call_count?: number;
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    reasoning_tokens?: number;
    total_tokens?: number;
  };
  turns?: {
    turn_num?: number | null;
    prompt_text?: string;
    response_text?: string;
    injected_skills?: string[];
    used_skills?: string[];
    tool_calls?: {
      id?: string;
      function?: { name?: string; arguments?: string };
    }[];
    tool_results?: {
      tool_call_id?: string;
      tool_name?: string;
      content?: string;
      has_error?: boolean;
    }[];
  }[];
}

export interface SessionProcess {
  cycles?: {
    timestamp?: string;
    sessions?: number | null;
    skill_groups?: number | null;
    actions?: number | null;
    skills_evolved?: number | null;
    uploaded_skills?: number | null;
    candidates_queued?: number | null;
    had_processing_error?: boolean;
    judge?: { overall_score?: number | null; rationale?: string };
    evolutions?: {
      skill_name?: string;
      action?: string;
      uploaded?: boolean;
      reason?: string;
      rationale?: string;
      evidence_classification?: EvidenceClassification;
      version?: number | null;
      job_id?: string;
      file_changes?: Array<{ path?: string; operation?: string; reason?: string }>;
    }[];
  }[];
}

export interface EvidenceClassification {
  team_skill?: Array<string | Record<string, unknown>>;
  user_memory?: Array<string | Record<string, unknown>>;
  task_requirement?: Array<string | Record<string, unknown>>;
  agent_runtime?: Array<string | Record<string, unknown>>;
  insufficient_evidence?: Array<string | Record<string, unknown>>;
}

export interface EvolveHistoryCycle {
  timestamp?: string;
  session_ids?: string[];
  sessions?: number | null;
  skill_groups?: number | null;
  actions?: number | null;
  skills_evolved?: number | null;
  uploaded_skills?: number | null;
  candidates_queued?: number | null;
  had_processing_error?: boolean;
  judge?: { overall_score?: number | null; rationale?: string };
  evolutions?: {
    skill_name?: string;
    action?: string;
    uploaded?: boolean;
    reason?: string;
    rationale?: string;
    evidence_classification?: EvidenceClassification;
    version?: number | null;
    job_id?: string;
    session_ids?: string[];
    file_changes?: Array<{ path?: string; operation?: string; reason?: string }>;
  }[];
  [key: string]: any;
}

export interface SessionFilterAuditItem {
  session_id: string;
  title?: string;
  user_alias?: string;
  status?: string;
  num_turns?: number;
  timestamp?: string;
  ingested_at?: string;
  recorded_at?: string;
  tool_call_count?: number;
  total_tokens?: number;
  value_judge?: {
    decision?: "valuable" | "chitchat" | string;
    confidence?: number;
    reason?: string;
    mode?: string;
    model?: string;
    true_replay_fallback_reason?: string;
  };
  candidate_skill?: Candidate["candidate_skill"];
  current_skill?: Candidate["current_skill"];
  current_skill_md?: string;
  candidate_skill_md?: string;
  skill_diff?: string;
}

export interface SessionFilterAuditResp {
  stats: {
    total: number;
    decisions?: Record<string, number>;
    statuses?: Record<string, number>;
    modes?: Record<string, number>;
  };
  items: SessionFilterAuditItem[];
  reason?: string;
}

// ---- Evolve model settings ---------------------------------------------- //

export interface EvolveModelSettings {
  provider?: string;
  base_url: string;
  model: string;
  max_tokens: number;
  temperature: number;
  api_key?: string;
  clear_api_key?: boolean;
  api_key_present?: boolean;
}

export interface EvolveModelTestResp {
  ok: boolean;
  model?: string;
  base_url?: string;
  latency_ms?: number;
  response?: string;
}

export interface EvolveProcessSettings {
  environment_overrides?: Record<string, string>;
  evolve: {
    use_session_judge: boolean;
    publish_mode: "direct" | "validated";
    validation_max_rejections: number;
    human_review_enabled: boolean;
    human_review_timeout_seconds: number;
    interval_seconds: number;
    evidence_enabled: boolean;
    evidence_max_entries: number;
    evidence_recent_limit: number;
    evidence_historical_limit: number;
    evidence_replay_cases_per_window: number;
    evidence_change_debt_threshold: number;
    dataset_synthesis_enabled: boolean;
    dataset_test_cases: number;
    dataset_min_requirements: number;
    dataset_max_requirements: number;
    dataset_disclosure_batch_size: number;
    candidate_coalesce_enabled: boolean;
    bundle_text_extensions: string[];
    bundle_max_file_bytes: number;
    bundle_max_prompt_bytes: number;
    bundle_allow_delete: boolean;
    bundle_static_checks_enabled: boolean;
  };
  validation: {
    enabled: boolean;
    mode: "replay" | "true_replay";
    idle_after_seconds: number;
    poll_interval_seconds: number;
    max_jobs_per_day: number;
    max_concurrency: number;
    required_results: number;
    required_approvals: number;
  };
  memory_maintenance: {
    enabled: boolean;
    auto_start: boolean;
    model: string;
    base_url: string;
    api_key?: string;
    clear_api_key?: boolean;
    api_key_present?: boolean;
    engine?: string;
    full_capabilities?: boolean;
    llm_max_tokens: number;
    temperature: number;
    agent_id?: string;
    customer_id?: string;
    maintained_space?: string;
    embed_model?: string;
    embed_base_url?: string;
    embed_api_key?: string;
    clear_embed_api_key?: boolean;
    embed_api_key_present?: boolean;
    semantic_dedup_enabled?: boolean;
    dedup_merge_threshold?: number;
    dedup_warn_threshold?: number;
    tools?: string[];
    scheduler: {
      active_start_hour: number;
      active_end_hour: number;
      rounds_per_window: number;
      round_interval_minutes: number;
      max_turns_per_job: number;
      max_consecutive_errors: number;
      retry_delay_seconds: number;
    };
    jobs: Array<{
      id: string;
      label: string;
      description: string;
      priority: number;
      enabled: boolean;
      overridden?: boolean;
      settings_overridden?: boolean;
      default_prompt: string;
      effective_prompt: string;
      runtime: {
        model: string;
        base_url: string;
        temperature: number;
        max_tokens: number;
        max_turns: number;
        max_errors: number;
      };
      default_runtime: {
        model: string;
        base_url: string;
        temperature: number;
        max_tokens: number;
        max_turns: number;
        max_errors: number;
      };
      tasks: Array<{
        id: string;
        description: string;
        priority: string;
      }>;
    }>;
    interval_seconds?: number;
    max_source_items?: number;
    max_source_chars?: number;
    prompts?: {
      extract: string;
      consolidate: string;
    };
  };
}

// ---- Skills management --------------------------------------------------- //

export interface SkillListItem {
  name: string;
  category?: string;
  description?: string;
  file_count?: number;
  updated_at?: string;
}

export interface SkillListResp {
  sharing_enabled?: boolean;
  skills: SkillListItem[];
}

export interface SkillDetail {
  name: string;
  category?: string;
  description?: string;
  body?: string;
  skill_md?: string;
  files?: string[];
}

// ---- Skills experiment lab --------------------------------------------- //

export interface SkillLabMaterial {
  path: string;
  size?: number;
  sha256?: string;
  content_b64?: string;
}

export interface SkillLabDataset {
  dataset_id: string;
  skill_name: string;
  name: string;
  query: string;
  requirements?: string;
  trajectory_requirements?: string;
  progressive_disclosure?: {
    enabled?: boolean;
    initial_visibility?: string;
    batch_size?: number;
    stop_when?: string;
  };
  materials?: SkillLabMaterial[];
  enabled_for_evolution?: boolean;
  material_integrity?: {
    status?: "complete" | "missing" | "not_required";
    mode?: "none" | "inline" | "uploaded" | "mixed" | "external";
    complete?: boolean;
    inline?: boolean;
    required_paths?: string[];
    available_paths?: string[];
    missing_paths?: string[];
    message?: string;
  };
  source?: {
    kind?: string;
    job_id?: string;
    session_id?: string;
    source_session_ids?: string[];
    turn_num?: number;
    evidence_window?: string;
    [key: string]: any;
  };
  read_only?: boolean;
  dataset_markdown?: string;
  created_at?: string;
  updated_at?: string;
}

export interface SkillLabTraceMessage {
  role?: string;
  content?: string | Array<Record<string, any>>;
  name?: string;
  tool_call_id?: string;
  tool_calls?: Array<{
    id?: string;
    function?: {
      name?: string;
      arguments?: string | Record<string, any>;
    };
  }>;
  [key: string]: any;
}

export interface SkillLabBranch {
  ok?: boolean;
  error?: string;
  elapsed_seconds?: number;
  interaction_turns?: number;
  tool_call_count?: number;
  total_tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  final_response?: string;
  trajectory?: string;
  messages?: SkillLabTraceMessage[];
  interactions?: Array<Record<string, any>>;
  artifacts?: Array<Record<string, any>>;
  checklist_report?: SkillLabChecklistReport;
}

export interface SkillLabChecklistReport {
  all_satisfied?: boolean;
  total?: number;
  satisfied_count?: number;
  unmet_count?: number;
  rounds?: number;
  judge?: string;
  items?: Array<{
    id?: string;
    text?: string;
    kind?: "output" | "trajectory" | string;
    satisfied?: boolean;
    evidence?: string;
  }>;
}

export interface SkillLabResult {
  status?: string;
  verdict?: "accept" | "reject" | "inconclusive";
  accepted?: boolean;
  reason?: string;
  harness?: { model?: string; base_url?: string };
  efficiency?: {
    baseline?: Record<string, number>;
    candidate?: Record<string, number>;
    dimensions?: Record<string, {
      baseline?: number;
      candidate?: number;
      delta?: number;
      reduction_ratio?: number;
      winner?: "candidate" | "baseline" | "tie";
    }>;
  };
  decision_policy?: ReplayDecisionPolicy;
  checklist?: {
    baseline?: SkillLabChecklistReport;
    candidate?: SkillLabChecklistReport;
  };
  cases?: Array<{
    baseline?: SkillLabBranch;
    candidate?: SkillLabBranch;
  }>;
}

export interface SkillLabRun {
  run_id: string;
  skill_name: string;
  dataset_id: string;
  dataset_name?: string;
  dataset_source?: Record<string, any>;
  candidate_skill_md?: string;
  candidate_skill_sha256?: string;
  timeout_seconds?: number;
  max_interactions?: number;
  status: "running" | "completed" | "failed" | "skipped" | string;
  result_summary?: {
    status?: string;
    verdict?: string;
    accepted?: boolean;
    reason?: string;
    efficiency?: SkillLabResult["efficiency"];
    harness?: SkillLabResult["harness"];
  };
  result?: SkillLabResult;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface CloudResult {
  synced?: boolean;
  reason?: string;
  action?: string;
  uploaded?: number;
  deleted?: boolean;
}

export function cloudNote(cloud?: CloudResult): string {
  if (!cloud || !cloud.synced) {
    if (cloud && cloud.reason === "sharing_disabled") return "未开启云端同步";
    if (cloud && cloud.reason) return "云端同步失败: " + cloud.reason;
    return "";
  }
  if (cloud.action === "delete")
    return cloud.deleted ? "已从云端删除" : "云端无此技能";
  return `已同步云端 (上传 ${cloud.uploaded || 0})`;
}

// ---- User management ----------------------------------------------------- //

export type SkillSpaceBackend = "" | "viking";

export interface SkillSpaceConfig {
  backend?: SkillSpaceBackend;
  viking_user?: string;
  viking_api_key?: string;
  clear_viking_api_key?: boolean;
  api_key_present?: boolean;
  inherited_from_admin?: boolean;
}

export interface UserProfile {
  id: string;
  display_name?: string;
  email?: string;
  role?: "user" | "admin";
  agent_identities?: Record<string, string>;
  agent_subjects?: Array<{
    integration_id: string;
    runtime_type: string;
    external_subject: string;
  }>;
  password?: string;
  password_set?: boolean;
  personal_space?: SkillSpaceConfig;
  team_space?: SkillSpaceConfig;
  created_at?: string;
  updated_at?: string;
}

export interface AgentIntegration {
  agent_id: string;
  runtime_type: string;
  display_name?: string;
  protocol_version?: string;
  runtime_version?: string;
  compatibility?: "compatible" | "legacy" | string;
  status?: string;
  capabilities?: string[];
  capability_ids?: string[];
  capability_details?: Record<string, Record<string, unknown>>;
  endpoints?: Record<string, string>;
  access_token_configured?: boolean;
  access_scopes?: string[];
  access_token_rotated_at?: string;
  updated_at?: string;
}

export interface AgentIntegrationsResp {
  agents: AgentIntegration[];
  storage_authority?: string;
  storage_deployment?: string;
}

export interface UsersListResp {
  users: UserProfile[];
}

export interface ShareResult {
  direction?: "personal_to_team" | "team_to_personal";
  uploaded?: number;
  skipped?: number;
  filtered?: number;
  total_local?: number;
  shared_names?: string[];
  missing_names?: string[];
}

// ---- Console auth -------------------------------------------------------- //

export interface AuthStatus {
  authenticated: boolean;
  needs_setup?: boolean;
  user?: UserProfile | null;
}

// ---- Langfuse session ingestion ----------------------------------------- //

export interface LangfuseStatus {
  enabled: boolean;
  tracing?: LangfuseTracingStatus;
  host?: string;
  public_key_present?: boolean;
  secret_key_present?: boolean;
  max_sessions?: number;
  reachable?: boolean;
  total_sessions?: number | null;
  reason?: string;
  default_filters?: {
    environment?: string[];
    user_id?: string;
    tags?: string[];
    release?: string;
    version?: string;
    trace_name?: string;
  };
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

// Session-attribute filters that can be applied to a Langfuse list/pull.
export interface LangfuseFilters {
  environment?: string[];
  user_id?: string;
  tags?: string[];
  release?: string;
  version?: string;
  trace_name?: string;
  session_id?: string;
  from_timestamp?: string;
  to_timestamp?: string;
  metadata?: Record<string, string>;
  max_sessions?: number;
}

export interface LangfuseSessionPreview {
  session_id: string;
  title?: string;
  timestamp?: string;
  trace_count?: number;
  user_id?: string;
  environment?: string;
  release?: string;
  version?: string;
  tags?: string[];
}

export interface LangfuseSessionsResp {
  filters?: Record<string, any>;
  count: number;
  sessions: LangfuseSessionPreview[];
}

export interface LangfusePullResultItem {
  session_id: string;
  status: string;
  queued?: boolean;
  turns?: number;
  reason?: string;
  value_judge?: {
    decision?: string;
    reason?: string;
    mode?: string;
    confidence?: number;
  };
}

export interface LangfusePullResp {
  filters?: Record<string, any>;
  total: number;
  counts: Record<string, number>;
  results: LangfusePullResultItem[];
}

// Persisted Langfuse connection + default-filter settings (editable in console).
export interface LangfuseConfig {
  enabled: boolean;
  tracing_enabled?: boolean;
  host: string;
  public_key?: string;
  public_key_present?: boolean;
  secret_key_present?: boolean;
  max_sessions?: number;
  page_limit?: number;
  timeout_seconds?: number;
  tracing_environment?: string;
  tracing_release?: string;
  tracing_sample_rate?: number;
  tracing_capture_content?: boolean;
  tracing_flush_at?: number;
  tracing_flush_interval_seconds?: number;
  tracing_status?: LangfuseTracingStatus;
  default_environment?: string[];
  default_user_id?: string;
  default_tags?: string[];
  default_release?: string;
  default_version?: string;
  default_trace_name?: string;
  // write-only fields (never returned by the server):
  secret_key?: string;
  clear_public_key?: boolean;
  clear_secret_key?: boolean;
}

export interface LangfuseTestResp {
  ok: boolean;
  host?: string;
  total_sessions?: number | null;
}

// ---- Prompt Studio (transparent skill-evolution pipeline) --------------- //

export interface PipelineNode {
  id: string;
  label: string;
  kind: "io" | "llm" | "logic" | "gate";
  description?: string;
  prompt_id?: string;
  overridden?: boolean;
}

export interface PipelineEdge {
  from: string;
  to: string;
}

export interface PipelineGraph {
  nodes: PipelineNode[];
  edges: PipelineEdge[];
}

export interface PromptSummary {
  id: string;
  label: string;
  description?: string;
  module?: string;
  symbol?: string;
  temperature?: number;
  max_tokens?: number;
  model?: string;
  settings_overridden?: boolean;
  injects_shared_blocks?: boolean;
  overridden?: boolean;
  char_count?: number;
  default_char_count?: number;
}

export interface PromptDetail extends PromptSummary {
  variables?: string[];
  default_prompt: string;
  effective_prompt: string;
  expanded_prompt?: string;
  shared_blocks?: Record<string, string>;
  default_temperature?: number;
  default_max_tokens?: number;
}

export interface PromptStudioSession {
  session_id: string;
  title?: string;
  user_alias?: string;
  num_turns?: number | null;
  status?: string;
  timestamp?: string;
}

export interface PromptTestResult {
  stage_id: string;
  system_prompt: string;
  user_message: string;
  output: string;
}
