import type { BundleDiff,Candidate,StaticValidation } from "@/features/skills/api/client";

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
    datasets?: ReplayDatasetResult[];
    sampling?: {
      current_case_count?: number;
      historical_requested?: number;
      historical_selected?: number;
      historical_dataset_count?: number;
    };
    efficiency?: {
      improved_dimensions?: string[];
      regressed_dimensions?: string[];
      unchanged_dimensions?: string[];
      dimensions?: Record<string, {
        baseline: number | "unavailable";
        candidate: number | "unavailable";
        delta: number | null;
        reduction_ratio: number | null;
        winner: "candidate" | "baseline" | "tie" | "unavailable";
      }>;
    };
    decision_policy?: ReplayDecisionPolicy;
    checklist?: {
      baseline?: WorkbenchChecklistReport;
      candidate?: WorkbenchChecklistReport;
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
  interaction_turns?: number | "unavailable" | null;
  tool_call_count?: number | "unavailable" | null;
  total_tokens?: number | "unavailable" | null;
  input_tokens?: number | "unavailable" | null;
  output_tokens?: number | "unavailable" | null;
  interactions?: Array<Record<string, any>>;
  checklist_report?: WorkbenchChecklistReport;
  trajectory?: string;
  messages?: WorkbenchTraceMessage[];
  artifacts?: Array<Record<string, any> | string>;
}

export interface ReplayCase {
  baseline?: ReplaySide;
  candidate?: ReplaySide;
  case_id?: string;
  query?: string;
  dataset_id?: string;
  dataset_name?: string;
  selection?: "current" | "historical";
  provenance?: Record<string, any>;
}

export interface ReplayDatasetResult {
  dataset_id: string;
  name: string;
  revision?: number;
  selection: "current" | "historical";
  total: number;
  completed: number;
  inputs: WorkbenchDataset["cases"];
  result: {
    verdict?: "accept" | "reject" | "inconclusive";
    reason?: string;
    cases?: ReplayCase[];
    efficiency?: NonNullable<EvalResult["replay"]>["efficiency"];
  };
}

export interface WorkbenchMaterial {
  path: string;
  size?: number;
  sha256?: string;
  content_b64?: string;
}

export interface WorkbenchDataset {
  schema_version: "team-replay.dataset";
  revision?: number;
  dataset_id: string;
  skills: Array<{
    skill_id: string;
    revision?: string | number;
    content_hash?: string;
  }>;
  name: string;
  description: string;
  cases: Array<{
    case_id: string;
    query: string;
    skill_ids: string[];
    checks: Array<{
      id: string;
      kind: "output" | "trajectory";
      text: string;
      polarity?: "must_not";
    }>;
    materials: WorkbenchMaterial[];
    provenance: {
      session_id?: string;
      session_ids?: string[];
      job_id?: string;
      evidence_window?: string;
      [key: string]: any;
    };
    replay: {
      progressive_disclosure: {
        enabled?: boolean;
        initial_visibility?: string;
        batch_size?: number;
        stop_when?: string;
      };
    };
    metadata: {
      name?: string;
      read_only?: boolean;
      enabled_for_evolution?: boolean;
      [key: string]: any;
    };
  }>;
  metadata: {
    read_only?: boolean;
    enabled_for_evolution?: boolean;
    [key: string]: any;
  };
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
    [key: string]: any;
  };
  dataset_markdown?: string;
  created_at?: string;
  updated_at?: string;
}

export interface WorkbenchTraceMessage {
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

export interface WorkbenchBranch {
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
  messages?: WorkbenchTraceMessage[];
  interactions?: Array<Record<string, any>>;
  artifacts?: Array<Record<string, any>>;
  checklist_report?: WorkbenchChecklistReport;
}

export interface WorkbenchChecklistReport {
  all_satisfied?: boolean;
  total?: number;
  satisfied_count?: number;
  unmet_count?: number;
  rounds?: number;
  judge?: string;
  /** Present on aggregated (multi-case) reports: one entry per replay case. */
  case_count?: number;
  reports?: WorkbenchChecklistReport[];
  items?: Array<{
    id?: string;
    text?: string;
    kind?: "output" | "trajectory" | string;
    satisfied?: boolean;
    evidence?: string;
  }>;
}

export interface WorkbenchResult {
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
    baseline?: WorkbenchChecklistReport;
    candidate?: WorkbenchChecklistReport;
  };
  cases?: Array<{
    baseline?: WorkbenchBranch;
    candidate?: WorkbenchBranch;
  }>;
}

export interface WorkbenchRun {
  run_id: string;
  subject_id?: string;
  job_id?: string;
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
    efficiency?: WorkbenchResult["efficiency"];
    harness?: WorkbenchResult["harness"];
  };
  result?: WorkbenchResult;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface ReplayAdapterInfo {
  tenant_id: string;
  file: string;
  configured: boolean;
  enabled: boolean;
  error?: string;
  revision?: string;
  source_editable?: boolean;
  available: Array<{ file: string; label?: string; enabled: boolean; error?: string }>;
}
