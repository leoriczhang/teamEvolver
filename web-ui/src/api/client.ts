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
      data?.detail || data?.raw || res.statusText || `${path} -> ${res.status}`
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
  endpoint?: string;
  namespace?: string;
  api_key_present?: boolean;
  reachable?: boolean;
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

export interface Candidate {
  job_id: string;
  skill_name?: string;
  candidate_skill_name?: string;
  proposed_action?: string;
  review_status?: string;
  rationale?: string;
  evidence_classification?: EvidenceClassification;
  content_preview?: string;
  min_score?: number;
  candidate_skill?: {
    name?: string;
    description?: string;
    category?: string;
    content?: string;
    edit_summary?: Record<string, any>;
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
  verify_score?: number | null;
  replay_score?: number | null;
  baseline_score?: number | null;
  recommended_publish?: boolean;
  evaluation_error?: string | null;
  evaluation?: EvalResult;
  decision?: CandidateDecision;
  decision_reason?: string;
  decided_at?: string;
  decision_accepted?: boolean | null;
  checklist?: ReplayChecklist;
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
  verify_score?: number | null;
  replay_score?: number | null;
  recommended_publish?: boolean;
  cached?: boolean;
  verification?: {
    threshold?: number | null;
    enabled?: boolean;
    accepted?: boolean;
    error?: string;
    decision?: string;
    reason?: string;
    checks?: Record<string, number | null>;
  };
  replay?: {
    threshold?: number | null;
    tolerance?: number | null;
    baseline_mean?: number | null;
    no_regression?: boolean;
    error?: string;
    cases?: ReplayCase[];
    efficiency?: {
      score?: number;
      weights?: Record<string, number>;
      improved_dimensions?: string[];
      regressed_dimensions?: string[];
      dimensions?: Record<string, {
        baseline: number;
        candidate: number;
        delta: number;
        reduction_ratio: number;
        weight?: number;
        weighted_gain?: number;
        winner: "candidate" | "baseline" | "tie";
      }>;
    };
    checklist?: ReplayChecklist;
    checklist_results?: Record<string, {
      baseline?: ChecklistEvaluation;
      candidate?: ChecklistEvaluation;
    }> | {
      baseline?: ChecklistEvaluation;
      candidate?: ChecklistEvaluation;
    };
    decision_policy?: ReplayDecisionPolicy;
    scoring_policy?: {
      quality?: string;
      efficiency_weights?: Record<string, number>;
      llm_role?: string;
    };
  };
  candidate_skill?: Candidate["candidate_skill"];
  current_skill?: Candidate["current_skill"];
  current_skill_md?: string;
  candidate_skill_md?: string;
  skill_diff?: string;
}

export interface ReplayChecklistItem {
  id: string;
  claim: string;
  kind: "hard" | "soft";
  evaluator: string;
  required?: boolean;
  source_session_ids?: string[];
  causal_link?: string;
  support_count?: number;
  passed?: boolean;
  observed_case_count?: number;
  inherited_from?: {
    skill_name?: string;
    version?: number | null;
  };
}

export interface ReplayChecklist {
  format?: string;
  action?: string;
  source_session_ids?: string[];
  source_user_aliases?: string[];
  controlled_profiles?: Record<string, string[]>;
  commonality?: {
    passed?: boolean;
    eligible_claim_count?: number;
    provisional_claim_count?: number;
    distinct_session_count?: number;
    distinct_user_count?: number;
  };
  items?: ReplayChecklistItem[];
  provisional_claims?: Array<Record<string, any>>;
  excluded_personal_evidence?: Array<any>;
  merge_context?: Record<string, any>;
}

export interface ChecklistEvaluation {
  passed?: boolean;
  hard_pass?: boolean;
  pass_rate?: number;
  hard_pass_rate?: number;
  soft_pass_rate?: number;
  items?: ReplayChecklistItem[];
}

export interface ReplayDecisionPolicy {
  accepted?: boolean;
  policy?: string;
  quality_gate?: boolean;
  commonality_pass?: boolean;
  no_regression?: boolean;
  coverage_gain?: number;
  turn_gain?: number;
  efficiency_score?: number;
  reason_codes?: string[];
  regressed_item_ids?: string[];
  merge_union_pass?: boolean;
  merge_source_results?: Array<{
    skill_name?: string;
    version?: number | null;
    inherited?: boolean;
    required_item_ids?: string[];
    failed_item_ids?: string[];
    passed?: boolean;
  }>;
  historical_no_regression?: boolean;
  all_windows_evaluated?: boolean;
}

export interface ReplaySide {
  score?: number | null;
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
  advisory_judge_score?: number | null;
  checklist?: ChecklistEvaluation;
}
export interface ReplayCase {
  baseline?: ReplaySide;
  candidate?: ReplaySide;
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
      verification?: VerificationResult;
      version?: number | null;
      job_id?: string;
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

export interface VerificationResult {
  threshold?: number | null;
  enabled?: boolean;
  accepted?: boolean;
  error?: string;
  decision?: string;
  reason?: string;
  score?: number | null;
  checks?: Record<string, number | null>;
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
    verification?: VerificationResult;
    version?: number | null;
    job_id?: string;
    session_ids?: string[];
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

export type SkillSpaceBackend = "" | "local" | "viking";

export interface SkillSpaceConfig {
  backend?: SkillSpaceBackend;
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
  password?: string;
  password_set?: boolean;
  personal_space?: SkillSpaceConfig;
  team_space?: SkillSpaceConfig;
  created_at?: string;
  updated_at?: string;
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
