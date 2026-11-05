
export type MinePage = "overview" | "sources" | "jobs";

export const PAGE_META: Record<MinePage, { title: string; description: string }> = {
  overview: { title: "挖掘总览", description: "查看知识源、挖掘任务与产物编译状态，快速掌握 SkillMiner 当前工作负载。" },
  sources: { title: "知识源", description: "统一登记 OpenViking 目录，并标记为 Skill 知识源、知识库知识源或同时用于两种挖掘；上传不会覆盖同名文件。" },
  jobs: { title: "Skill 挖掘", description: "从标记为 Skill 知识源的目录创建和跟踪挖掘任务；完成后可审核、编辑产物并提交进化。" },
};

// ---- Native OpenViking mining API types ---------------------------------- //

export interface InputSource {
  source_id?: string;
  account?: string;
  name?: string;
  path: string;
  document_count: number;
  total_bytes: number;
  ready: boolean;
  status?: "ready" | "empty" | "missing" | "over_limit" | string;
  error?: string;
  ingestion?: { error?: string };
  tags?: Array<"skill" | "knowledge" | string>;
  default?: boolean;
}

export interface MiningArtifact {
  name: string;
  kind: "skill" | "evaluation" | "benchmark" | "semantic" | "resource";
  path: string;
  size_bytes: number;
  skill_name: string;
  display_title?: string;
}

export interface MiningRound {
  round: number;
  artifacts: MiningArtifact[];
  status?: string;
  started_at?: string;
  finished_at?: string;
  task_id?: string;
  ov_task_id?: string;
  remote_warnings?: string[];
}

export interface MiningArtifactQualityItem {
  kind: "skill" | "evaluation" | "benchmark";
  state: "ready" | "caution" | "incomplete";
  label: string;
  detail: string;
}

export interface MiningArtifactQuality {
  has_skill: boolean;
  level: "ready" | "caution" | "incomplete";
  label: string;
  confidence: "生产级" | "候选级" | "草稿级" | "unknown" | string;
  summary: string;
  warnings: string[];
  artifacts: MiningArtifactQualityItem[];
  can_submit: boolean;
}

export interface MiningCapability {
  name: string;
  task: string;
  trigger: string;
  inputs: string[];
  outputs: string[];
  boundary: string;
  rationale: string;
  source_refs: string[];
}

export interface MiningCapabilityPlan {
  schema_version: string;
  skills: MiningCapability[];
  deferred: Array<{ reason: string; source_refs: string[] }>;
}

export interface CandidateSubmissionResult {
  skillName: string;
  state: "created" | "existing" | "failed";
  error?: string;
}

export type MiningJobStatus =
  | "preparing"
  | "queued"
  | "running"
  | "waiting_human"
  | "stopping"
  | "stopped"
  | "succeeded"
  | "failed"
  | "interrupted";

export interface MiningJob {
  job_id: string;
  name: string;
  mining_intent?: { mode: "auto" | "single" | "target"; direction: string; target_count: number | null };
  can_resume?: boolean;
  can_retry_native?: boolean;
  native_retry_context?: {
    expected_task_id: string;
    budget_seconds: number;
    format_guidance_sha256: string;
  } | null;
  native_explicit_retry?: { attempt_budget: { seconds: number; started_at?: string; deadline_at?: string } } | null;
  status: MiningJobStatus;
  input_dir: string;
  document_count: number | null;
  max_rounds: number;
  current_round: number | null;
  runtime_timeout_seconds?: number;
  round_timeout_seconds?: number;
  round_work_units?: number;
  staged_progress?: { phase: string; label: string; completed: number; total: number; subtask_index?: number };
  execution_protocol?: string;
  native_compile_history?: { round: number; task_id: string }[];
  native_repairs?: { round: number; attempt: number; error: string }[];
  ov_cancel_error?: string;
  phase: PhaseState;
  created_at: string;
  started_at: string;
  finished_at: string;
  round_completed_at?: string;
  updated_at: string;
  error: string;
  stop_reason: string;
  artifacts?: MiningArtifact[];
  rounds?: MiningRound[];
  logs?: string[];
  pending_checkpoint?: HumanCheckpoint | null;
  skipped_checkpoints?: Array<{ round: number; reason: string }>;
  knowledge_gaps?: {
    total: number;
    questions: HumanCheckpointQuestion[];
  } | null;
  knowledge_supplements?: KnowledgeSupplementRound[];
  artifact_quality?: MiningArtifactQuality | null;
  candidate_contract_version?: number;
  capability_plan?: MiningCapabilityPlan | null;
  candidate_count?: number;
  candidate_quality?: Record<string, MiningArtifactQuality>;
  ov_status?: string;
  ov_stage?: string;
  ov_last_synced_at?: string;
  ov_last_sync_attempt_at?: string;
  ov_sync_error?: string;
  remote_warnings?: string[];
  ov_pipeline?: { errors?: unknown };
  ov_sync_failures?: number;
  ov_next_retry_at?: string;
  remote_state_stale?: boolean;
  submission_seconds?: number;
  compile_observed_seconds?: number;
  materialize_seconds?: number;
  materialize_started_at?: string;
  ov_stage_history?: Array<{
    stage: string;
    first_seen_at: string;
    last_seen_at: string;
    observations: number;
  }>;
  ov_event_timeline?: Array<{ label: string; at: string; status: string }>;
}

export interface HumanCheckpointQuestion {
  qid: string;
  dimension: string;
  severity: string;
  question: string;
  context?: string;
  source?: string;
  field_label?: string;
  placeholder?: string;
  answer_type?: "short_text" | "long_text";
  required?: boolean;
}

export interface HumanCheckpoint {
  id: string;
  checkpoint: string;
  round: number;
  title: string;
  intro: string;
  questions: HumanCheckpointQuestion[];
  allow_stop?: boolean;
  attachments?: EvidenceAttachment[];
}

export interface EvidenceAttachment {
  name: string;
  uri: string;
}

export interface KnowledgeSupplementRound {
  round: number;
  checkpoint: string;
  title: string;
  intro?: string;
  questions: HumanCheckpointQuestion[];
  answers?: Record<string, string>;
  unresolved?: string[];
  references?: Record<string, string[]>;
  feedback?: string;
  attachments?: EvidenceAttachment[];
  submitted_at?: string;
}

export interface MiningJobSummary {
  total: number;
  running: number;
  queued: number;
  completed: number;
  failed: number;
  max_parallel: number;
}

export interface ArtifactContent {
  path: string;
  name: string;
  content: string;
  size_bytes: number;
}

export interface MiningDefaults {
  max_rounds_default: number;
  max_rounds_range: [number, number];
  runtime_timeout_seconds_default: number;
  runtime_timeout_seconds_range: [number, number];
}

export interface MiningConfig extends MiningDefaults {
  input_dirs: string[];
  input_sources: InputSource[];
  default_input_dir: string;
}

export interface PhaseState {
  step1: "idle" | "active" | "done";
  step2: "idle" | "active" | "done";
  step3: "idle" | "active" | "done";
}
