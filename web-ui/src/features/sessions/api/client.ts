import type { Candidate,EvidenceClassification } from "@/features/skills/api/client";

export interface PageResponse<T> {
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  sessions?: T[];
  conversations?: T[];
  candidates?: T[];
}

export interface SessionMeta {
  user_id?: string;
  session_id?: string;
  trace_id?: string;
}

export interface LedgerRow {
  session_id: string;
  title?: string;
  user_alias?: string;
  /** Adapter-extracted business meta (user_id / session_id / trace_id). */
  meta?: SessionMeta;
  num_turns?: number | null;
  status?: string;
  consumed_at?: string;
  ingested_at?: string;
  timestamp?: string;
  used_skills?: string[];
  value_judge?: {
    decision?: string;
    confidence?: number;
    reason?: string;
  };
  judge?: {
    overall_score?: number | null;
    rationale?: string;
    reasons?: SessionJudgeReasons;
    judged_at?: string;
    task_completion?: number | null;
    response_quality?: number | null;
    efficiency?: number | null;
    tool_usage?: number | null;
  };
}

export interface SessionDetail {
  /** Conversation summary (title / submitter / status / turns). */
  summary?: {
    title?: string;
    user_alias?: string;
    status?: string;
    num_turns?: number | null;
  };
  /** Adapter-extracted business meta, same shape as the list rows. */
  meta?: SessionMeta;
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
  value_judge?: {
    decision?: string;
    confidence?: number;
    reason?: string;
  };
  judge?: SessionJudgeDetail;
}

export interface SessionJudgeReasons {
  task_completion?: string[];
  response_quality?: string[];
  efficiency?: string[];
  tool_usage?: string[];
}

export interface SessionJudgeDetail {
  overall_score?: number | null;
  rationale?: string;
  reasons?: SessionJudgeReasons;
  task_completion?: number | null;
  response_quality?: number | null;
  efficiency?: number | null;
  tool_usage?: number | null;
}

export interface SessionProcess {
  stages?: Array<{ stage: string; status: string; created_at?: string; updated_at?: string; error_code?: string }>;
  cycles?: {
    timestamp?: string;
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
      file_changes?: Array<{ path?: string; operation?: string; reason?: string }>;
    }[];
  }[];
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
