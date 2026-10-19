import type { EvalResult } from "@/features/replay/api/client";
import type { SessionJudgeDetail } from "@/features/sessions/api/client";

export function hydrateCandidates(
  items: Candidate[],
  details: Record<string, Candidate>
): Candidate[] {
  if (!Object.keys(details).length) return items;
  return items.map((item) => {
    const d = details[item.job_id];
    if (!d) return item;
    const merged: any = { ...item };
    for (const [k, v] of Object.entries(d)) {
      if (v === undefined || v === null) continue;
      if (merged[k] === undefined || merged[k] === null || merged[k] === "") {
        merged[k] = v;
      }
    }
    return d.feedback ? mergeCandidateFeedback(merged as Candidate, d.feedback) : merged as Candidate;
  });
}

export function mergeCandidateFeedback(candidate: Candidate, feedback: CandidateFeedback): Candidate {
  return (candidate.feedback?.version ?? -1) > feedback.version
    ? candidate
    : { ...candidate, feedback };
}

export interface StatusResp {
  role?: string;
  ready?: boolean;
  running?: boolean;
  pending_sessions?: number;
  registered_skills?: number;
  skills?: Record<string, { skill_id?: string; version?: number }>;
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

export type CandidateFeedbackField = "reviewed" | "adopted" | "rejected";

export interface CandidateFeedback {
  reviewed: boolean;
  adopted: boolean;
  rejected: boolean;
  version: number;
  updated_at?: string;
  history: Array<{
    field: CandidateFeedbackField;
    value: boolean;
    actor_id: string;
    actor_name: string;
    at: string;
    candidate_revision: number;
    version: number;
  }>;
}

export interface Candidate {
  job_id: string;
  skill_name?: string;
  candidate_skill_name?: string;
  proposed_action?: string;
  review_status?: string;
  replay_progress?: { total: number; completed: number; succeeded: number; failed: number };
  feedback?: CandidateFeedback;
  rationale?: string;
  evidence_classification?: EvidenceClassification;
  content_preview?: string;
  source?: {
    kind?: string;
    skill_name?: string;
    artifact_sha256?: string;
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
  judge?: SessionJudgeDetail;
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

export interface SkillExperience {
  id: string;
  skill_name: string;
  kind: "defect" | "exemplary";
  experience_key?: string;
  description: string;
  occurrence_count: number;
  session_ids?: string[];
  user_aliases?: string[];
  first_observed_at?: string;
  last_observed_at?: string;
  last_ingested_at?: string;
  latest_score?: number | null;
}

export interface SkillExperienceListResp {
  items: SkillExperience[];
  stats: {
    total_experiences?: number;
    total_occurrences?: number;
    defect_experiences?: number;
    defect_occurrences?: number;
    exemplary_experiences?: number;
    exemplary_occurrences?: number;
    skills?: number;
  };
  skill_counts?: Record<string, number>;
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  reason?: string;
}

export interface SkillExperiencePatternOutcome {
  job_id: string;
  status: string;
  recorded_at?: string;
  candidate_revision?: number;
  version?: number;
  reason?: string;
}

export interface SkillExperiencePattern {
  pattern_id: string;
  pattern_key: string;
  skill_name: string;
  title: string;
  kind: "defect" | "exemplary";
  problem?: string;
  root_cause?: string;
  guidance: string;
  experience_ids?: string[];
  session_ids?: string[];
  user_aliases?: string[];
  evidence_count: number;
  occurrence_count: number;
  session_count: number;
  user_count: number;
  team_ready: boolean;
  status: "ready" | "observing";
  candidate_outcomes?: SkillExperiencePatternOutcome[];
  compiled_at?: string;
  updated_at?: string;
}

export interface SkillExperiencePatternListResp {
  items: SkillExperiencePattern[];
  stats: {
    total_patterns?: number;
    ready_patterns?: number;
    observing_patterns?: number;
    defect_patterns?: number;
    exemplary_patterns?: number;
    source_experiences?: number;
    skills?: number;
  };
  skill_counts?: Record<string, number>;
  available_skills?: string[];
  location: string;
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface SkillExperiencePatternCompileResp {
  compiled: boolean;
  skill_name: string;
  patterns: SkillExperiencePattern[];
  pattern_count: number;
  compiled_at?: string;
  location: string;
}

export interface EvolveModelSettings {
  tenant_id?: string;
  scope?: "global" | "tenant";
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
  revision?: number;
  evolve: Record<string, boolean | number | string | string[]>;
  validation: Record<string, boolean | number | string>;
}

export interface SkillListItem {
  name: string;
  skill_id?: string;
  version?: number;
  category?: string;
  description?: string;
  file_count?: number;
  updated_at?: string;
}

export interface SkillListResp {
  skill_backend?: "postgres" | "viking" | "local" | string;
  delivery_mode?: "pull";
  skills: SkillListItem[];
}

export interface SkillDetail {
  sha256?: string;
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
    if (cloud && cloud.reason) return "云端同步失败: " + cloud.reason;
    return "";
  }
  if (cloud.action === "delete")
    return cloud.deleted ? "已从云端删除" : "云端无此技能";
  return `已同步云端 (上传 ${cloud.uploaded || 0})`;
}

export interface PipelineNode {
  id: string;
  label: string;
  kind: "io" | "llm" | "logic" | "gate";
  description?: string;
  prompt_id?: string;
  overridden?: boolean;
  conditional?: boolean;
  join?: "all";
  parallel_group?: string;
  branch?: "skill" | "dataset";
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
  base_url?: string;
  api_key_present?: boolean;
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
  /** Agent-loop stages (evolve/create/merge) also return a per-round transcript. */
  rounds?: Array<{
    round: number;
    type: string;
    action?: string;
    tools?: string[];
    errors?: string[];
    error?: string;
  }>;
}
