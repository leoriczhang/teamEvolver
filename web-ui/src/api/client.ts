export type { MemoryReplayBranch, MemoryTrueReplay, MemoryChangeRecord, MemoryChangeList, MemoryTrueReplayList, AggregationSettings, AggregationSettingsUpdate } from "@memory/types";
// API client for the teamEvolver unified console.
//
// All requests are same-origin: the 52010 evolve server hosts this SPA and
// serves dashboard, auth, user, skill, model-config and session-ingest endpoints natively.
// In dev, vite.config.ts proxies these paths to 127.0.0.1:52010.

import { toastErr } from "@/lib/toast";

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

// ---- Tenant context (multi-tenancy plan §4.1) ----------------------------- //
// The console admin selects a tenant; the choice survives reloads via
// localStorage and the ?tenant= URL param (URL wins so shared links are
// exact). The header is only a selector — the backend still authenticates
// the session and re-validates the tenant against its registry.

const TENANT_STORAGE_KEY = "teamEvolver.activeTenantId";

// Header injection is gated on the session being an admin console user —
// the backend rejects X-Tenant-Id for non-admins, and a stale selection
// left by a previous admin login would otherwise 403 every request.
let tenantHeaderAllowed = false;

export function setTenantHeaderAllowed(allowed: boolean): void {
  tenantHeaderAllowed = allowed;
  if (!allowed) {
    // Keep the stored selection (the user may re-login as admin) but stop
    // sending it until an admin session is confirmed again.
  }
}

export function getActiveTenantId(): string {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("tenant");
    if (fromUrl) return fromUrl;
    return window.localStorage.getItem(TENANT_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setActiveTenantId(tenantId: string): void {
  try {
    if (tenantId) window.localStorage.setItem(TENANT_STORAGE_KEY, tenantId);
    else window.localStorage.removeItem(TENANT_STORAGE_KEY);
    const url = new URL(window.location.href);
    if (tenantId) url.searchParams.set("tenant", tenantId);
    else url.searchParams.delete("tenant");
    window.history.replaceState(null, "", url.toString());
  } catch {
    // localStorage/URL unavailable (private mode etc.) — selection is lost.
  }
}

// Clear an invalid selection (e.g. tenant was disabled or deleted while
// the console was open) so subsequent requests fall back to default.
export function clearInvalidTenantSelection(message: string): void {
  const current = getActiveTenantId();
  if (current && current !== "default") {
    setActiveTenantId("");
  }
  toastErr("租户不可用，已切回默认租户", message);
}

/**
 * Build the tenant selector header for the given path.
 *
 * The header is only a selector — the backend still authenticates the
 * session and re-validates the tenant against its registry.  Injection is
 * gated on the session being an admin console user (see
 * {@link setTenantHeaderAllowed}).  Covers every non-`/v1/` endpoint so that
 * dashboard endpoints (`/status`, `/conversations`, `/storage/status`, …)
 * are also tenant-scoped, not just `/api/` and `/langfuse/`.
 *
 * Returns a `Headers` (or the passed-in headers mutated) so callers that use
 * raw `fetch()` instead of {@link api} can still attach the header.
 */
export function tenantHeaders(path: string, init?: HeadersInit): Headers {
  const headers = init instanceof Headers ? init : new Headers(init);
  const tenantId = getActiveTenantId();
  if (
    tenantId &&
    tenantHeaderAllowed &&
    !path.startsWith("/v1/") &&
    !headers.has("X-Tenant-Id")
  ) {
    headers.set("X-Tenant-Id", tenantId);
  }
  return headers;
}

// Merge full candidate details into a polled (compact) candidate list.
// Poll endpoints return list items without heavy fields (candidate_skill_md,
// skill_diff, bundle_diff, ...); a blind replace would blank an open detail
// modal. Fresh list values win; cached detail only fills missing keys.
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

// FastAPI validation errors are arrays; never stringify their input/ctx fields,
// which may contain an entire submitted document or credential.
function apiErrorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(apiErrorMessage).filter(Boolean).join("；");
  if (detail && typeof detail === "object") {
    const value = detail as Record<string, unknown>;
    const message = [value.msg, value.message, value.code].find(v => typeof v === "string");
    const location = Array.isArray(value.loc)
      ? value.loc.filter(v => typeof v === "string" || typeof v === "number").join(".") : "";
    if (message) return location ? `${location}: ${message}` : String(message);
  }
  return "";
}

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  // Every request must settle: the backend serves the SPA and the evolution
  // cycle on one event loop, so under load a request can hang for minutes.
  // A hung fetch wedges view polling (inflight guards never clear) — bound
  // GETs (polls) to 20s and mutations to 120s so the UI always recovers.
  const method = (opts?.method || "GET").toUpperCase();
  // Auth checks should be fast, but the backend shares one event loop with
  // the evolution cycle — under load even a trivial /api/auth/status can
  // hang.  Give auth paths more headroom so a busy server doesn't log the
  // user out with a scary timeout toast.
  const isAuthPath = path.startsWith("/api/auth/");
  const isDatasourceJob = path === "/api/datasource/pull" || path === "/api/datasource/sessions";
  const timeoutMs = isDatasourceJob ? 3_600_000 : isAuthPath ? 60_000 : method === "GET" ? 20_000 : 120_000;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = opts?.signal
    ? AbortSignal.any([opts.signal, timeoutSignal])
    : timeoutSignal;
  let res: Response;
  try {
    // Tenant selector header (console admin only — the server rejects it for
    // non-admin sessions and derives the tenant from the token otherwise).
    // Covers every non-/v1/ endpoint so dashboard endpoints (/status,
    // /conversations, /storage/status, …) are also tenant-scoped.
    const headers = tenantHeaders(path, opts?.headers);
    res = await fetch(path, { ...opts, headers, signal });
  } catch (err: any) {
    if (err?.name === "TimeoutError" || err?.name === "AbortError") {
      throw new ApiError(`请求超时（${timeoutMs / 1000}s），稍后自动重试`);
    }
    throw err;
  }
  const text = await res.text();
  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    // A tenant selection the backend rejects (disabled/deleted tenant, or a
    // tampered ?tenant= param) is auto-cleared once so the console recovers.
    if (res.status === 403 || res.status === 401) {
      const detail = String(data?.detail || "");
      if (
        detail === "tenant mismatch" ||
        detail.startsWith("unknown tenant:") ||
        detail === "admin required for tenant switch" ||
        detail === "login required"
      ) {
        clearInvalidTenantSelection(detail);
      }
      // Session expired in PG mode — reload so /api/auth/status re-checks
      // and the LoginGate renders. Guard against reload loops by only
      // triggering when not already on the login surface.
      if (
        res.status === 401 &&
        (detail === "login or tenant token required" || detail === "login required") &&
        !window.location.pathname.startsWith("/api/auth/")
      ) {
        window.location.reload();
        return new Promise(() => {}); // never resolves — page is reloading
      }
    }
    throw new ApiError(
      apiErrorMessage(data?.detail) || apiErrorMessage(data?.msg) || data?.raw || res.statusText || `${path} -> ${res.status}`,
      res.status
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
  fallback_enabled?: boolean;
  effective_backend?: string;
  fallback_active?: boolean;
  local_root?: string;
  session_backend?: string;
  skill_backend?: string;
  mirror_enabled?: boolean;
  mirror?: {
    enabled?: boolean;
    spool_dir?: string;
    backlog?: number;
    oldest_age_seconds?: number;
    dead_letter?: number;
    last_error?: string;
    error?: string;
  };
  // Postgres-backed storage pool health (Phase 3; present only when a PG
  // backend is configured for the selected tenant).
  pg?: {
    reachable?: boolean;
    ping_ms?: number | null;
    pool_size?: number;
    pool_idle?: number;
    pool_min?: number;
    pool_max?: number;
    tenant_id?: string;
  };
  // Present on the sharing-config save response: OpenViking directory skeleton
  // check/create result for the just-saved connection.
  openviking_dirs?: VikingDirsReport;
}

export type VikingDeployment = "cloud" | "local";

// Result of an OpenViking directory bootstrap run (POST
// /api/sharing-config/bootstrap-dirs and the sharing-config save response).
export interface VikingDirsReport {
  action?: "ok" | "partial" | "failed";
  account_id?: string;
  endpoint?: string;
  checked?: number;
  created?: string[];
  existing?: string[];
  errors?: Array<{ uri: string; error: string }>;
  error?: string;
}

export interface SharingConfig {
  enabled?: boolean;
  backend?: string;
  deployment?: VikingDeployment;
  endpoint?: string;
  endpoint_override?: string;
  cloud_endpoint?: string;
  local_endpoint?: string;
  account?: string;
  team_user?: string;
  root_prefix?: string;
  service_api_key_present?: boolean;
  team_api_key_present?: boolean;
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
  feedback?: CandidateFeedback;
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
  tenant_id?: string;
  scope?: "global" | "tenant";
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
  memory_maintenance?: {
    enabled: boolean;
    auto_start: boolean;
    engine?: string;
    full_capabilities?: boolean;
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
  skills?: Array<{
    skill_id: string;
    revision?: string | number;
    content_hash?: string;
  }>;
  skill_ids?: string[];
  task_count?: number;
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
  /** Present on aggregated (multi-case) reports: one entry per replay case. */
  case_count?: number;
  reports?: SkillLabChecklistReport[];
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
  bound?: boolean;
}

export interface OpenVikingAccountsResp {
  accounts: string[];
  current: string;
  source?: "openviking" | "fallback";
  endpoint?: string;
  error?: string;
}

export interface OpenVikingAccountUser {
  user_id: string;
  role: "user" | "admin";
  openviking_role?: string;
  imported?: boolean;
}

export interface OpenVikingAccountUsersResp {
  account: string;
  users: OpenVikingAccountUser[];
  source?: "openviking" | "fallback";
  endpoint?: string;
  error?: string;
}

export interface ImportAccountUsersResp {
  account: string;
  imported: string[];
  skipped_existing: string[];
  missing: string[];
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
  updated_at?: string;
}

export interface AgentIntegrationsResp {
  agents: AgentIntegration[];
  storage_authority?: string;
  storage_deployment?: string;
  default_team_user?: string;
}

export interface RegisterAgentPayload {
  agent_id: string;
  runtime_type: string;
  runtime_version?: string;
  display_name?: string;
  replay_url: string;
  /** "server_driven"（teamEvolver 逐轮调 Agent）或 "branch_delegate"（旧单次调用） */
  orchestration?: string;
  max_interactions?: number;
  /** 留空 = 回放调用不带 Bearer 密钥（内网可信部署） */
  auth_profile?: string;
  session_ingest?: boolean;
  /** 零感知模式：按此模板渲染每轮请求体（占位符 {{prompt}}/{{request_id}}/{{history}} 等） */
  request_template?: Record<string, unknown>;
  /** 零感知模式：从 Agent 自身响应提取字段的点路径映射 */
  response_mapping?: Record<string, string>;
}

export interface AgentRegistrationResp {
  agent: AgentIntegration;
  subject_sync?: unknown;
}

export async function registerAgentIntegration(
  payload: RegisterAgentPayload,
): Promise<AgentRegistrationResp> {
  return api<AgentRegistrationResp>("/api/agent-integrations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export interface UsersListResp {
  storage_deployment?: string;
  openviking_account?: string;
  default_team_user?: string;
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

export interface PublishRequest {
  request_id: string;
  requester_id: string;
  requester_name?: string;
  skill_names: string[];
  note?: string;
  status: "pending" | "approved" | "rejected";
  created_at?: string;
  updated_at?: string;
  decided_by?: string;
  decided_at?: string;
  decision_note?: string;
  result?: ShareResult;
}

export interface PublishRequestListResp {
  requests: PublishRequest[];
  pending_count: number;
}

// ---- Console auth -------------------------------------------------------- //

export interface AuthStatus {
  customer_mode?: boolean;
  authenticated: boolean;
  needs_setup?: boolean;
  user?: UserProfile | null;
}

// ---- Tenant management (multi-tenancy plan §4) ---------------------------- //

export interface TenantInfo {
  tenant_id: string;
  display_name: string;
  status: string; // active | disabled
}

export interface TenantsResp {
  mode: "postgres" | "single";
  /** Single-tenant deployments configure one credential instead of issuing per-tenant ones. */
  machine_credential?: {
    configured: boolean;
    env_var: string;
    prefix: string;
  };
  tenants: TenantInfo[];
}

export interface OpenvikingAccountBinding {
  account_id: string;
  action: "created" | "bound" | "failed";
  error?: string;
}

export interface CreateTenantResp {
  tenant: TenantInfo;
  // Plaintext agent token — returned exactly once; only its sha256 is stored.
  agent_token: string;
  openviking_account: OpenvikingAccountBinding;
}

export async function listTenants(): Promise<TenantsResp> {
  return api<TenantsResp>("/api/tenants");
}

export async function createTenant(displayName: string, accountId = ""): Promise<CreateTenantResp> {
  return api<CreateTenantResp>("/api/tenants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, account_id: accountId }),
  });
}

export async function rotateTenantToken(tenantId: string): Promise<{ agent_token: string }> {
  return api<{ agent_token: string }>(`/api/tenants/${encodeURIComponent(tenantId)}/rotate-token`, {
    method: "POST",
  });
}

export async function setTenantStatus(
  tenantId: string,
  status: "active" | "disabled",
): Promise<{ tenant_id: string; status: string }> {
  return api<{ tenant_id: string; status: string }>(
    `/api/tenants/${encodeURIComponent(tenantId)}/status`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
}

export interface TenantConfigResp {
  tenant_id: string;
  display_name: string;
  // Flat TeamEvolverConfig-field-name overrides stored in tenants.config.
  config_overrides: Record<string, unknown>;
  secret_presence?: Record<string, boolean>;
  // Every key the server accepts in a config update (field names + quotas).
  editable_keys: string[];
}

export async function getTenantConfig(tenantId: string): Promise<TenantConfigResp> {
  return api<TenantConfigResp>(`/api/tenants/${encodeURIComponent(tenantId)}/config`);
}

// `overrides` merges into tenants.config: non-null values upsert, null deletes.
export async function updateTenantConfig(
  tenantId: string,
  overrides: Record<string, unknown>,
): Promise<{ tenant_id: string; config_overrides: Record<string, unknown>; secret_presence?: Record<string, boolean> }> {
  return api<{ tenant_id: string; config_overrides: Record<string, unknown>; secret_presence?: Record<string, boolean> }>(
    `/api/tenants/${encodeURIComponent(tenantId)}/config`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides }),
    },
  );
}

// ---- Service-wide outbound observability ------------------------------- //

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

export interface LangfuseTracingConfig {
  enabled: boolean;
  host: string;
  public_key_present?: boolean;
  secret_key_present?: boolean;
  environment?: string;
  release?: string;
  sample_rate?: number;
  capture_content?: boolean;
  flush_at?: number;
  flush_interval_seconds?: number;
  status?: LangfuseTracingStatus;
  // Write-only fields; the server returns presence flags instead.
  public_key?: string;
  secret_key?: string;
  clear_public_key?: boolean;
  clear_secret_key?: boolean;
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
  provider?: string;
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
