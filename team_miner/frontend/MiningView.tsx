import { useCallback, useEffect, useRef, useState } from "react";
import { PageHeader, Panel, StatCard, Pill, type PillTone } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, type UserProfile } from "@/api/client";
import { fileToB64 } from "@/lib/file";
import { createRequestId } from "@/lib/requestId";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { markdownBodyForPreview } from "@/lib/markdownFrontmatter";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import MiningWhiteboxPanel from "@miner/MiningWhiteboxPanel";
import SkillSourceWorkspace from "@miner/SkillSourceWorkspace";
import {
  Boxes,
  ScanSearch,
  FileCode2,
  RotateCcw,
  Play,
  Upload,
  CheckCircle2,
  ArrowRight,
  FileText,
  Eye,
  FolderPlus,
  Trash2,
  ListChecks,
  Clock3,
  CircleStop,
  ChevronDown,
  Pencil,
  Save,
  Send,
  ShieldAlert,
} from "lucide-react";

/**
 * MiningView — SkillMiner「文档 → Skill」挖掘流水线控制台。
 *
 * 集成后 teamEvolver 统一控制台「挖掘」分组的落地页。挖掘能力在左侧边栏拆成
 * 3 个独立菜单项（总览 / 知识源 / 挖掘任务），本组件
 * 按 `page` 渲染对应页面。
 *
 * TE 原生 API 管理 OpenViking 知识源、持久化挖掘任务和每轮人工 Evidence 补充。
 * OpenViking Compile 负责候选编译，任务完成后通过 Skill Evolution 提交候选。
 */

export type MinePage = "overview" | "sources" | "jobs";

const PAGE_META: Record<MinePage, { title: string; description: string }> = {
  overview: { title: "挖掘总览", description: "查看知识源、挖掘任务与产物编译状态，快速掌握 SkillMiner 当前工作负载。" },
  sources: { title: "知识源", description: "统一登记 OpenViking 目录，并标记为 Skill 知识源、知识库知识源或同时用于两种挖掘；上传不会覆盖同名文件。" },
  jobs: { title: "Skill 挖掘", description: "从标记为 Skill 知识源的目录创建和跟踪挖掘任务；完成后可审核、编辑产物并提交进化。" },
};

// ---- Native OpenViking mining API types ---------------------------------- //

interface InputSource {
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

interface MiningArtifact {
  name: string;
  kind: "skill" | "evaluation" | "benchmark" | "semantic" | "resource";
  path: string;
  size_bytes: number;
  skill_name: string;
  display_title?: string;
}

interface MiningRound {
  round: number;
  artifacts: MiningArtifact[];
  status?: string;
  started_at?: string;
  finished_at?: string;
  task_id?: string;
  ov_task_id?: string;
  remote_warnings?: string[];
}

interface MiningArtifactQualityItem {
  kind: "skill" | "evaluation" | "benchmark";
  state: "ready" | "caution" | "incomplete";
  label: string;
  detail: string;
}

interface MiningArtifactQuality {
  has_skill: boolean;
  level: "ready" | "caution" | "incomplete";
  label: string;
  confidence: "生产级" | "候选级" | "草稿级" | "unknown" | string;
  summary: string;
  warnings: string[];
  artifacts: MiningArtifactQualityItem[];
  can_submit: boolean;
}

interface MiningCapability {
  name: string;
  task: string;
  trigger: string;
  inputs: string[];
  outputs: string[];
  boundary: string;
  rationale: string;
  source_refs: string[];
}

interface MiningCapabilityPlan {
  schema_version: string;
  skills: MiningCapability[];
  deferred: Array<{ reason: string; source_refs: string[] }>;
}

interface CandidateSubmissionResult {
  skillName: string;
  state: "created" | "existing" | "failed";
  error?: string;
}

type MiningJobStatus =
  | "preparing"
  | "queued"
  | "running"
  | "waiting_human"
  | "stopping"
  | "stopped"
  | "succeeded"
  | "failed"
  | "interrupted";

interface MiningJob {
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

interface HumanCheckpointQuestion {
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

interface HumanCheckpoint {
  id: string;
  checkpoint: string;
  round: number;
  title: string;
  intro: string;
  questions: HumanCheckpointQuestion[];
  allow_stop?: boolean;
  attachments?: EvidenceAttachment[];
}

interface EvidenceAttachment {
  name: string;
  uri: string;
}

interface KnowledgeSupplementRound {
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

interface MiningJobSummary {
  total: number;
  running: number;
  queued: number;
  completed: number;
  failed: number;
  max_parallel: number;
}

interface ArtifactContent {
  path: string;
  name: string;
  content: string;
  size_bytes: number;
}

interface MiningDefaults {
  max_rounds_default: number;
  max_rounds_range: [number, number];
  runtime_timeout_seconds_default: number;
  runtime_timeout_seconds_range: [number, number];
}

interface MiningConfig extends MiningDefaults {
  input_dirs: string[];
  input_sources: InputSource[];
  default_input_dir: string;
}

interface PhaseState {
  step1: "idle" | "active" | "done";
  step2: "idle" | "active" | "done";
  step3: "idle" | "active" | "done";
}

const STEP_META = [
  { key: "step1" as const, n: 1, title: "输入与提交", sub: "冻结 Evidence 并提交 Compile", icon: Boxes },
  { key: "step2" as const, n: 2, title: "OV Compile", sub: "来源解析与候选编译", icon: ScanSearch },
  { key: "step3" as const, n: 3, title: "产物复核", sub: "结构校验与 Evidence 补充", icon: FileCode2 },
];

/**
 * Shared hook: owns knowledge-source configuration and the persistent job list.
 * It polls lightweight task summaries; the selected active task separately
 * polls its detail so logs and pipeline progress stay current.
 */
function useMining(active: boolean) {
  const [config, setConfig] = useState<MiningConfig | null>(null);
  const [jobs, setJobs] = useState<MiningJob[]>([]);
  const [jobSummary, setJobSummary] = useState<MiningJobSummary>({
    total: 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3,
  });
  const jobsRefreshInFlight = useRef<Promise<MiningJob[]> | null>(null);
  const configRefreshInFlight = useRef<Promise<MiningConfig | null> | null>(null);

  const refreshJobs = useCallback((notifyFailure = false) => {
    if (jobsRefreshInFlight.current) return jobsRefreshInFlight.current;
    const request = (async () => {
      try {
        const response = await api<{ jobs: MiningJob[]; summary: MiningJobSummary }>("/api/mining/jobs");
        setJobs(response.jobs || []);
        setJobSummary(response.summary || {
          total: 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3,
        });
        return response.jobs || [];
      } catch (e: any) {
        if (notifyFailure) toastErr("加载挖掘任务失败", e.message);
        return [];
      }
    })().finally(() => {
      jobsRefreshInFlight.current = null;
    });
    jobsRefreshInFlight.current = request;
    return request;
  }, []);

  const refreshConfig = useCallback((notifyFailure = true) => {
    if (configRefreshInFlight.current) return configRefreshInFlight.current;
    const request = (async () => {
      try {
        const [base, ovSources] = await Promise.all([
          api<MiningDefaults>("/api/mining/config"),
          api<{ sources: InputSource[] }>("/api/mining/input-sources"),
        ]);
        const sources = ovSources.sources || [];
        const skillSources = sources.filter((source) => source.tags?.includes("skill"));
        const next: MiningConfig = {
          ...base,
          input_sources: sources,
          input_dirs: skillSources.map((source) => source.path),
          default_input_dir: skillSources.find((source) => source.ready)?.path || skillSources[0]?.path || "",
        };
        setConfig(next);
        await refreshJobs(false);
        return next;
      } catch (e: any) {
        if (notifyFailure) toastErr("加载挖掘配置失败", e.message);
        return null;
      }
    })().finally(() => {
      configRefreshInFlight.current = null;
    });
    configRefreshInFlight.current = request;
    return request;
  }, [refreshJobs]);

  const refreshInputSource = useCallback(async (sourceId: string) => {
    const response = await api<{ source: InputSource }>(
      `/api/mining/input-sources/${encodeURIComponent(sourceId)}`
    );
    setConfig((current) => current ? {
      ...current,
      input_sources: current.input_sources.map((source) =>
        source.source_id === sourceId ? response.source : source
      ),
    } : current);
    return response.source;
  }, []);

  useEffect(() => {
    if (!active) return;
    // Job summaries are local control-plane state and do not wait for OV tree scans.
    refreshJobs(false);
    refreshConfig(true);
    const jobsTimer = window.setInterval(() => refreshJobs(false), 5000);
    return () => {
      window.clearInterval(jobsTimer);
    };
  }, [active, refreshConfig, refreshJobs]);

  return { config, jobs, jobSummary, refreshConfig, refreshJobs, refreshInputSource };
}
export default function MiningView({
  active,
  page,
  preferredInputDir,
  onInputDirChange,
  onNavigate,
  user: _user,
}: {
  active: boolean;
  page: MinePage;
  preferredInputDir?: string;
  onInputDirChange?: (path: string) => void;
  onNavigate?: (destination: MinePage | "candidates" | "skills") => void;
  user?: UserProfile | null;
}) {
  const mining = useMining(active);
  const { config } = mining;

  const [rounds, setRounds] = useState(3);
  const [runtimeTimeout, setRuntimeTimeout] = useState(3600);
  const [humanCheckpoints, setHumanCheckpoints] = useState(true);
  const [taskName, setTaskName] = useState("");
  const [miningMode, setMiningMode] = useState<"auto" | "single" | "target">("auto");
  const [miningDirection, setMiningDirection] = useState("");
  const [targetSkillCount, setTargetSkillCount] = useState(3);
  const [starting, setStarting] = useState(false);
  const [selectedInputDir, setSelectedInputDir] = useState("");
  const [jobFilter, setJobFilter] = useState<"all" | "active" | "completed" | "failed">("all");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [selectedJob, setSelectedJob] = useState<MiningJob | null>(null);
  const [jobActionPending, setJobActionPending] = useState(false);
  const nativeRetryPending = useRef(false);
  const nativeRetryRequest = useRef<{ signature: string; id: string } | null>(null);
  const [deleteJob, setDeleteJob] = useState<MiningJob | null>(null);
  const [jobDeleting, setJobDeleting] = useState(false);
  const [checkpointAnswers, setCheckpointAnswers] = useState<Record<string, string>>({});
  const [checkpointUnresolved, setCheckpointUnresolved] = useState<Record<string, boolean>>({});
  const [checkpointReferences, setCheckpointReferences] = useState<Record<string, string>>({});
  const [checkpointFeedback, setCheckpointFeedback] = useState("");
  const [checkpointAttachments, setCheckpointAttachments] = useState<EvidenceAttachment[]>([]);
  const [checkpointUploading, setCheckpointUploading] = useState(false);
  const [checkpointSubmitting, setCheckpointSubmitting] = useState(false);
  const checkpointRequest = useRef<{ signature: string; id: string } | null>(null);
  const activeCheckpoint = useRef({ jobId: selectedJobId, checkpointId: selectedJob?.pending_checkpoint?.id });
  activeCheckpoint.current = { jobId: selectedJobId, checkpointId: selectedJob?.pending_checkpoint?.id };
  const [knowledgeSupplementExpanded, setKnowledgeSupplementExpanded] = useState(false);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactContent | null>(null);
  const [artifactJobId, setArtifactJobId] = useState("");
  const [artifactDraft, setArtifactDraft] = useState("");
  const [artifactEditing, setArtifactEditing] = useState(false);
  const [artifactEditorMode, setArtifactEditorMode] = useState<"edit" | "preview">("edit");
  const [artifactEditable, setArtifactEditable] = useState(false);
  const [artifactSaving, setArtifactSaving] = useState(false);
  const [loadingArtifact, setLoadingArtifact] = useState("");
  const [submittingJobId, setSubmittingJobId] = useState("");
  const submissionPending = useRef(false);
  const [candidateSelections, setCandidateSelections] = useState<Record<string, string[]>>({});
  const [submissionReports, setSubmissionReports] = useState<Record<string, CandidateSubmissionResult[]>>({});
  const [createJobOpen, setCreateJobOpen] = useState(false);
  const [checkingInputSource, setCheckingInputSource] = useState(false);
  const [inputSourceCheckError, setInputSourceCheckError] = useState("");
  const artifactIsMarkdown = isMarkdownArtifact(artifactPreview);
  const defaultsInitialized = useRef(false);

  // Refresh defaults while the form is closed; preserve choices during editing.
  useEffect(() => {
    if (!config) return;
    if ((!defaultsInitialized.current || !createJobOpen) && config.max_rounds_default) {
      setRounds(config.max_rounds_default);
      setRuntimeTimeout(config.runtime_timeout_seconds_default || 3600);
      defaultsInitialized.current = true;
    }
    const next = preferredInputDir && config.input_dirs.includes(preferredInputDir)
      ? preferredInputDir
      : selectedInputDir && config.input_dirs.includes(selectedInputDir)
        ? selectedInputDir
        : config.default_input_dir || config.input_dirs[0] || "";
    setSelectedInputDir(next);
    onInputDirChange?.(next);
  }, [config, preferredInputDir, createJobOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active || page !== "jobs" || !selectedJobId) return;
    let cancelled = false;
    let inFlight = false;
    async function loadDetail() {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await api<{ ok: boolean; job: MiningJob }>(
          `/api/mining/jobs/${encodeURIComponent(selectedJobId)}`
        );
        if (!cancelled) setSelectedJob(response.job);
      } catch (e: any) {
        if (!cancelled) toastErr("加载任务详情失败", e.message);
      } finally {
        inFlight = false;
      }
    }
    loadDetail();
    const selected = mining.jobs.find((job) => job.job_id === selectedJobId);
    const timer = selected && isActiveJob(selected.status)
      ? window.setInterval(loadDetail, 5000)
      : undefined;
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
  }, [active, page, selectedJobId, mining.jobs]);

  useEffect(() => {
    setCheckpointAnswers({});
    setCheckpointUnresolved({});
    setCheckpointReferences({});
    setCheckpointFeedback("");
    setCheckpointAttachments([]);
    checkpointRequest.current = null;
  }, [selectedJob?.pending_checkpoint?.id]);

  useEffect(() => {
    setKnowledgeSupplementExpanded(Boolean(selectedJob?.pending_checkpoint));
  }, [selectedJobId, selectedJob?.pending_checkpoint?.id]);

  if (!active) return null;

  const meta = PAGE_META[page];
  const inputDir = selectedInputDir || config?.default_input_dir || "";
  const maxRounds = config?.max_rounds_range?.[1] ?? 5;
  const minRounds = config?.max_rounds_range?.[0] ?? 1;
  const inputSources = config?.input_sources ?? [];
  const skillSources = inputSources.filter((source) => source.tags?.includes("skill"));
  const selectedSource = skillSources.find((source) => source.path === inputDir);
  const selectedSourceError = selectedSource?.ingestion?.error || selectedSource?.error;
  const jobCounts = {
    total: mining.jobs.length,
    running: mining.jobs.filter((job) => job.status === "running" || job.status === "stopping").length,
    waiting: mining.jobs.filter((job) => isWaitingForEvidence(job)).length,
    queued: mining.jobs.filter((job) => job.status === "queued" || job.status === "preparing").length,
    completed: mining.jobs.filter((job) => job.status === "succeeded").length,
    failed: mining.jobs.filter((job) => job.status === "failed" || job.status === "interrupted" || job.status === "stopped").length,
  };
  const sourceDocumentTotal = inputSources.reduce((sum, source) => sum + source.document_count, 0);
  const hasUnverifiedSources = inputSources.some((source) => source.status === "unverified");
  const pipelineJobs = mining.jobs.filter(
    (job) => job.status === "running" || isWaitingForEvidence(job) || job.status === "stopping"
  );
  const recentTaskCount = Math.min(5, jobCounts.total);
  const running = jobCounts.running > 0 || jobCounts.queued > 0;
  const stateLabel = [
    jobCounts.running ? `${jobCounts.running} 个运行中` : "",
    jobCounts.queued ? `${jobCounts.queued} 个排队中` : "",
    jobCounts.waiting ? `${jobCounts.waiting} 个等待补证` : "",
  ].filter(Boolean).join(" · ") || "当前无运行任务";
  const stateTone = jobCounts.running ? "blue" : jobCounts.queued ? "amber" : "gray";
  const filteredJobs = mining.jobs.filter((job) => {
    if (jobFilter === "active") return isActiveJob(job.status);
    if (jobFilter === "completed") return job.status === "succeeded";
    if (jobFilter === "failed") return job.status === "failed" || job.status === "interrupted" || job.status === "stopped";
    return true;
  });

  function selectInputDir(path: string) {
    setSelectedInputDir(path);
    setInputSourceCheckError("");
    onInputDirChange?.(path);
  }

  async function verifySelectedInputSource() {
    if (!selectedSource?.source_id) return;
    setCheckingInputSource(true);
    setInputSourceCheckError("");
    try {
      const source = await mining.refreshInputSource(selectedSource.source_id);
      if (!source.ready) {
        setInputSourceCheckError(source.ingestion?.error || source.error || "该目录当前没有可挖掘文档");
      }
    } catch (e: any) {
      setInputSourceCheckError(e.message || "来源检查失败");
    } finally {
      setCheckingInputSource(false);
    }
  }

  async function startRun() {
    setStarting(true);
    try {
      const response = await api<{ jobs: MiningJob[] }>("/api/mining/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: taskName.trim() || `${selectedSource?.path.split("/").pop() || "知识源"} 挖掘任务`,
          source_id: selectedSource?.source_id || "",
          input_dir: inputDir,
          max_rounds: rounds,
          human_checkpoints: humanCheckpoints,
          runtime_timeout_seconds: runtimeTimeout,
          mining_mode: miningMode,
          mining_direction: miningDirection.trim(),
          target_skill_count: miningMode === "single" ? 1 : miningMode === "target" ? targetSkillCount : null,
        }),
      });
      const created = response.jobs?.[0];
      if (created) setSelectedJobId(created.job_id);
      await mining.refreshJobs(false);
      setTaskName("");
      setCreateJobOpen(false);
      toastOk("挖掘任务已创建", `输入 ${inputDir} · 最多 ${rounds} 轮`);
      onNavigate?.("jobs");
    } catch (e: any) {
      toastErr("启动挖掘失败", e.message);
    } finally {
      setStarting(false);
    }
  }

  async function resumeJob(job: MiningJob) {
    setJobActionPending(true);
    try {
      const response = await api<{ job: MiningJob }>(`/api/mining/jobs/${encodeURIComponent(job.job_id)}/resume`,
        { method: "POST" });
      setSelectedJob(response.job);
      await mining.refreshJobs(false);
      toastOk("已从失败断点恢复", "保留当前轮输入、已完成产物与未解决问题。");
    } catch (e: any) {
      toastErr("恢复任务失败", e.message);
    } finally {
      setJobActionPending(false);
    }
  }

  async function retryNativeJob(job: MiningJob) {
    const context = nativeRetryContext(job);
    if (!context || jobActionPending || nativeRetryPending.current) return;
    nativeRetryPending.current = true;
    setJobActionPending(true);
    try {
      const signature = JSON.stringify([job.job_id, context]);
      if (nativeRetryRequest.current?.signature !== signature) {
        nativeRetryRequest.current = { signature, id: createRequestId() };
      }
      const response = await api<{ job: MiningJob }>(`/api/mining/jobs/${encodeURIComponent(job.job_id)}/resume`, {
        method: "POST",
        body: JSON.stringify({
          action: "retry_with_new_budget",
          request_id: nativeRetryRequest.current.id,
          expected_task_id: context.expected_task_id,
          budget_seconds: context.budget_seconds,
        }),
      });
      setSelectedJob(response.job);
      await mining.refreshJobs(false);
      toastOk("已重新实测本轮", "保留原任务和历史，本次使用单独记录的运行期限。");
    } catch (e: any) {
      toastErr("重新实测失败", e.message);
    } finally {
      nativeRetryPending.current = false;
      setJobActionPending(false);
    }
  }

  async function stopJob(job: MiningJob) {
    setJobActionPending(true);
    try {
      const response = await api<{ job: MiningJob }>(
        `/api/mining/jobs/${encodeURIComponent(job.job_id)}/stop`,
        { method: "POST" }
      );
      setSelectedJob(response.job);
      await mining.refreshJobs(false);
      toastOk("已发送停止信号", job.name);
    } catch (e: any) {
      toastErr("停止任务失败", e.message);
    } finally {
      setJobActionPending(false);
    }
  }

  async function deleteMiningJob() {
    if (!deleteJob) return;
    setJobDeleting(true);
    try {
      const result = await api<{ job_id: string; deleted_files: number }>(
        `/api/mining/jobs/${encodeURIComponent(deleteJob.job_id)}`,
        { method: "DELETE" }
      );
      if (selectedJobId === deleteJob.job_id) {
        setSelectedJobId("");
        setSelectedJob(null);
      }
      setDeleteJob(null);
      await mining.refreshJobs(false);
      toastOk("挖掘任务已删除", `已一并删除 ${result.deleted_files || 0} 个任务文件和产物`);
    } catch (e: any) {
      toastErr("删除挖掘任务失败", e.message);
    } finally {
      setJobDeleting(false);
    }
  }

  async function uploadCheckpointEvidence(job: MiningJob, files: File[]) {
    const checkpoint = job.pending_checkpoint;
    if (!checkpoint || !files.length) return;
    setCheckpointUploading(true);
    try {
      const prepared = await Promise.all(files.map(async (file) => ({ name: file.name, content_base64: await fileToB64(file) })));
      const response = await api<{ saved: EvidenceAttachment[]; job: MiningJob }>(
        `/api/mining/jobs/${encodeURIComponent(job.job_id)}/supplements`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ checkpoint_id: checkpoint.id, files: prepared }),
        }
      );
      if (activeCheckpoint.current.jobId !== job.job_id || activeCheckpoint.current.checkpointId !== checkpoint.id) return;
      setCheckpointAttachments((current) => [...current, ...(response.saved || [])]);
      if (response.job) setSelectedJob(response.job);
      toastOk("已准备补充 Evidence", "继续下一轮时会固化到本轮输入；结束挖掘时保留在补证记录中。");
    } catch (e: any) {
      toastErr("上传补充 Evidence 失败", e.message);
    } finally {
      setCheckpointUploading(false);
    }
  }

  async function submitCheckpointAnswers(job: MiningJob, action: "continue" | "finish" = "continue") {
    const checkpoint = job.pending_checkpoint;
    if (!checkpoint || checkpointSubmitting || checkpointUploading) return;
    if (action === "continue" && checkpoint.round >= job.max_rounds) return;
    const missing = checkpoint.questions.find((question) => (
      question.required && !checkpointUnresolved[question.qid] && !checkpointAnswers[question.qid]?.trim()
    ));
    if (missing) {
      toastErr("请确认必填 Evidence 问题", `请回答“${missing.question}”，或明确勾选暂时无法确认；来源引用不能代替回答。`);
      return;
    }
    setCheckpointSubmitting(true);
    try {
      const references = Object.fromEntries(checkpoint.questions.map((question) => [
        question.qid,
        Array.from(new Set((checkpointReferences[question.qid] || "").split("\n").map((uri) => uri.trim()).filter(Boolean))),
      ]));
      const unresolved = checkpoint.questions.filter((question) => (
        checkpointUnresolved[question.qid] || (!question.required && !checkpointAnswers[question.qid]?.trim())
      )).map((question) => question.qid);
      const answers = Object.fromEntries(Object.entries(checkpointAnswers).filter(([qid, answer]) => !unresolved.includes(qid) && answer.trim()));
      const payload = {
        checkpoint_id: checkpoint.id,
        answers,
        unresolved,
        references,
        feedback: checkpointFeedback,
      };
      const signature = JSON.stringify({ job_id: job.job_id, action, ...payload });
      if (checkpointRequest.current?.signature !== signature) {
        checkpointRequest.current = { signature, id: createRequestId() };
      }
      const response = await api<{ job: MiningJob }>(
        `/api/mining/jobs/${encodeURIComponent(job.job_id)}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...payload,
            request_id: checkpointRequest.current.id,
          }),
        }
      );
      setSelectedJob(response.job);
      setCheckpointAnswers({});
      await mining.refreshJobs(false);
      toastOk(action === "finish" ? "挖掘已结束" : "补证信息已提交", action === "finish"
        ? "最终产物和未解决的证据缺口已保留，可继续审核。"
        : "下一轮将使用累计补充的 Evidence 和复核意见。");
    } catch (e: any) {
      toastErr("提交知识补证失败", e.message);
    } finally {
      setCheckpointSubmitting(false);
    }
  }

  async function previewArtifact(artifact: MiningArtifact, editable: boolean) {
    setLoadingArtifact(artifact.path);
    try {
      const result = await api<ArtifactContent>(
        `/api/mining/artifacts/content?path=${encodeURIComponent(artifact.path)}`
      );
      setArtifactPreview(result);
      setArtifactJobId(selectedJobId);
      setArtifactDraft(result.content);
      setArtifactEditing(false);
      setArtifactEditorMode("edit");
      setArtifactEditable(editable);
    } catch (e: any) {
      toastErr("读取历史产物失败", e.message);
    } finally {
      setLoadingArtifact("");
    }
  }

  async function saveArtifactRevision() {
    if (!artifactPreview) return;
    const editedJobId = artifactJobId;
    const editedPath = artifactPreview.path;
    setArtifactSaving(true);
    try {
      const saved = await api<ArtifactContent & { edited: boolean }>(
        "/api/mining/artifacts/content",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: artifactPreview.path, content: artifactDraft }),
        }
      );
      setArtifactPreview(saved);
      setArtifactDraft(saved.content);
      setArtifactEditing(isMarkdownArtifact(saved));
      const pathParts = editedPath.split("/");
      const skillIndex = pathParts.indexOf("compiled_skill");
      const editedSkillName = skillIndex >= 0 ? pathParts[skillIndex + 1] : "";
      setSubmissionReports((previous) => ({
        ...previous,
        [editedJobId]: editedSkillName
          ? (previous[editedJobId] || []).filter((item) => item.skillName !== editedSkillName)
          : [],
      }));
      toastOk("产物修改已保存", "继续挖掘或提交到进化时将使用当前版本");
      try {
        const response = await api<{ job: MiningJob }>(`/api/mining/jobs/${encodeURIComponent(editedJobId)}`);
        if (activeCheckpoint.current.jobId === editedJobId) setSelectedJob(response.job);
      } catch (error: any) {
        toastErr("产物已保存，刷新候选状态失败", error.message);
      }
    } catch (e: any) {
      toastErr("保存产物失败", e.message);
    } finally {
      setArtifactSaving(false);
    }
  }

  async function submitJobToEvolution(job: MiningJob, requestedNames = jobSkillNames(job)) {
    if (job.status !== "succeeded" || submissionPending.current) return;
    const quality = job.artifact_quality;
    if (quality && !quality.can_submit) {
      toastErr("当前产物尚不能提交", quality.warnings[0] || "请先补齐评测定义后再提交到进化。");
      return;
    }
    const skillNames = jobSkillNames(job).filter((name) => requestedNames.includes(name) && candidateCanSubmit(job, name));
    if (!skillNames.length) {
      toastErr("无法提交到进化", "请先选择可提交的 Skill Candidate");
      return;
    }
    const selectedQualities = skillNames.flatMap((name) => job.candidate_quality?.[name] ? [job.candidate_quality[name]] : []);
    const cautiousQuality = [...selectedQualities, ...(quality ? [quality] : [])].find((item) => item.level !== "ready");
    if (cautiousQuality) {
      const warning = distinctWarnings([
        ...(quality?.warnings || []),
        ...selectedQualities.flatMap((item) => item.warnings),
      ]).slice(0, 3).join("\n") || cautiousQuality.summary;
      if (!window.confirm(`所选产物标记为“${cautiousQuality.label}”。\n\n${warning}\n\n仍要提交到进化候选区吗？`)) {
        return;
      }
    }
    submissionPending.current = true;
    setSubmittingJobId(job.job_id);
    try {
      const results: CandidateSubmissionResult[] = [];
      for (const skillName of skillNames) {
        const skillArtifact = jobArtifacts(job).find(
          (artifact) => artifact.kind === "skill" && artifact.skill_name === skillName
        );
        try {
          const result = await api<{ created: boolean; job_id: string; skill_name: string }>(
            `/api/mined-jobs/${encodeURIComponent(job.job_id)}/skills/${encodeURIComponent(skillName)}/submit`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ artifact_path: skillArtifact?.path || "" }),
            }
          );
          results.push({ skillName, state: result.created ? "created" : "existing" });
        } catch (error: any) {
          results.push({ skillName, state: "failed", error: error.message || "提交失败" });
        }
      }
      setSubmissionReports((previous) => ({
        ...previous,
        [job.job_id]: [
          ...(previous[job.job_id] || []).filter((item) => !skillNames.includes(item.skillName)),
          ...results,
        ],
      }));
      const created = results.filter((result) => result.state === "created").length;
      const existing = results.filter((result) => result.state === "existing").length;
      const failed = results.filter((result) => result.state === "failed").length;
      const summary = `${created} 个新建，${existing} 个当前版本已在候选区，${failed} 个失败`;
      if (failed) {
        toastErr(created + existing ? "部分 Skill Candidate 提交成功" : "提交到进化失败", summary);
      } else {
        toastOk("已提交到进化候选区", summary);
        if (job.candidate_contract_version !== 2 && jobSkillNames(job).length === 1) onNavigate?.("candidates");
      }
    } finally {
      submissionPending.current = false;
      setSubmittingJobId("");
    }
  }

  function renderJobDetail(job: MiningJob) {
    const durableGaps = job.knowledge_gaps?.questions || [];
    const supplementRounds = job.knowledge_supplements || [];
    const supplementQuestions = job.pending_checkpoint?.questions || durableGaps;
    const hasKnowledgeSupplement = Boolean(job.pending_checkpoint) || supplementQuestions.length > 0 || supplementRounds.length > 0;
    const checkpointBusy = checkpointSubmitting || checkpointUploading;
    const currentRemoteWarnings = distinctWarnings([
      ...(job.remote_warnings || []),
      ...pipelineWarningMessages(job.ov_pipeline?.errors),
      ...((job.rounds || []).find((round) => round.round === job.current_round)?.remote_warnings || []),
    ]);
    const compileIssues = compileIssueKinds([...currentRemoteWarnings, job.error]);
    const roundWarningMessages = (job.rounds || []).flatMap((round) => distinctWarnings(round.remote_warnings));
    const finalRemoteWarnings = distinctWarnings([
      ...currentRemoteWarnings.filter((warning) => !roundWarningMessages.includes(warning)),
      ...(job.rounds || []).flatMap((round) => distinctWarnings(round.remote_warnings).map((warning) => `第 ${round.round} 轮：${warning}`)),
    ]);
    const checkpointFiles = Array.from(new Map([
      ...(job.pending_checkpoint?.attachments || []), ...checkpointAttachments,
    ].map((file) => [file.uri, file])).values());
    const compileSeconds = job.compile_observed_seconds ?? elapsedSeconds(job.started_at, job.finished_at);
    const roundTimeoutSeconds = job.round_timeout_seconds || job.runtime_timeout_seconds;
    const retryContext = nativeRetryContext(job);
    const nativeRetrySeconds = job.native_explicit_retry
      ? elapsedSeconds(job.native_explicit_retry.attempt_budget.started_at,
        isExecutingJob(job.status) ? undefined : job.round_completed_at || job.finished_at)
      : null;
    const hasTimingData = Boolean(
      job.started_at || job.submission_seconds !== undefined || job.materialize_seconds !== undefined || job.native_explicit_retry
    );
    const skillNames = jobSkillNames(job);
    const eligibleNames = skillNames.filter((name) => candidateCanSubmit(job, name));
    const selectedNames = (candidateSelections[job.job_id] || eligibleNames).filter((name) => eligibleNames.includes(name));
    const candidateReport = submissionReports[job.job_id] || [];
    const failedNames = candidateReport.filter((item) => item.state === "failed" && eligibleNames.includes(item.skillName)).map((item) => item.skillName);
    const showCandidateGroups = job.candidate_contract_version === 2 || skillNames.length > 1;
    const artifactGroups = groupMiningArtifacts(jobArtifacts(job), skillNames);
    const editableArtifacts = job.status === "succeeded" || isWaitingForEvidence(job);
    return (
      <div className="space-y-4 border-t border-accent/20 bg-accent-soft/35 px-5 py-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="select-text truncate text-[15px] font-bold">{job.name}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <Pill tone={jobStatusMeta(job.status).tone as any}>{jobStatusMeta(job.status).label}</Pill>
              {job.input_dir && <span className="mono select-text">{job.input_dir}</span>}
              {job.document_count !== null && <span>{job.document_count} 个文档</span>}
              <span>第 {job.current_round || 1} / {job.max_rounds} 轮</span>
              {job.mining_intent && <span>
                {job.mining_intent.mode === "auto" ? "自由挖掘"
                  : job.mining_intent.mode === "single" ? "单 Skill 整合"
                  : `目标 ${job.mining_intent.target_count} 个 Skill`}
              </span>}
              {currentRemoteWarnings.length > 0 && <Pill tone="amber">Compile 执行需复核</Pill>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {retryContext && <Button size="sm" disabled={jobActionPending} onClick={() => retryNativeJob(job)}>
              重新实测本轮
            </Button>}
            {job.can_resume && <Button size="sm" disabled={jobActionPending} onClick={() => resumeJob(job)}>
              从断点恢复
            </Button>}
            {isActiveJob(job.status) && (
              <Button size="sm" variant="outline" disabled={jobActionPending || checkpointBusy || job.status === "stopping"} onClick={() => stopJob(job)}>
                <CircleStop className="size-3.5" /> {job.status === "stopping" ? "停止中" : "停止"}
              </Button>
            )}
            {!isActiveJob(job.status) && (
              <Button size="sm" variant="outline" className="text-destructive hover:border-destructive/50 hover:bg-destructive/5 hover:text-destructive" onClick={() => setDeleteJob(job)}>
                <Trash2 className="size-3.5" /> 删除任务
              </Button>
            )}
          </div>
        </div>

        {retryContext && <p className="text-xs text-muted-foreground">
          保留原任务和历史，新增一次 {Number((retryContext.budget_seconds / 60).toFixed(1))} 分钟运行期限；用于保护预算，不代表预计完成时间。
        </p>}

        {job.mining_intent?.direction && <div className="rounded-lg border border-border bg-background p-3 text-xs">
          <span className="font-semibold">本次挖掘方向：</span>{job.mining_intent.direction}
        </div>}

        {!job.pending_checkpoint && finalRemoteWarnings.length > 0 && (
          <RemoteCompileWarnings warnings={finalRemoteWarnings} label="任务 Compile 执行提示" historical />
        )}

        {job.remote_state_stale && (
          <div className="flex items-start gap-2.5 rounded-xl border border-amber-300/70 bg-amber-50/80 px-4 py-3 text-[12px] text-amber-900">
            <ShieldAlert className="mt-0.5 size-4 shrink-0" />
            <div>
              <div className="font-bold">远端状态暂不可用</div>
              <div className="mt-0.5 leading-relaxed">
                页面正在显示本地保存的最后已知状态，任务记录不会丢失；系统会按退避间隔自动重连。
                {job.ov_last_synced_at ? ` 上次同步：${formatDateTime(job.ov_last_synced_at)}。` : ""}
              </div>
              {job.ov_sync_error && <div className="mono mt-1 break-all text-[10px] text-amber-800">{job.ov_sync_error}</div>}
            </div>
          </div>
        )}

        {hasTimingData && (
          <div className="rounded-xl border border-border bg-background p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="text-[13px] font-bold">耗时分析</div>
              {job.ov_stage && <span className="mono text-[10.5px] text-muted-foreground">当前阶段：{job.ov_stage}</span>}
              {job.native_explicit_retry
                ? <span>人工重试 · 本次运行期限：{formatDuration(job.native_explicit_retry.attempt_budget.seconds)}</span>
                : roundTimeoutSeconds && <span>运行期限：{formatDuration(roundTimeoutSeconds)}</span>}
              {job.ov_cancel_error && <span className="text-destructive">取消请求未确认：{job.ov_cancel_error}，后台继续重试</span>}
            </div>
            {job.staged_progress?.label && (
              <div className="mb-3 text-[12px] text-muted-foreground">
                {job.staged_progress.label} · 累计已校验 {job.staged_progress.completed} 个小任务
              </div>
            )}
            {job.execution_protocol === "te-native-mining.v1" && (
              <div className="mb-3 text-[12px] text-muted-foreground">
                整轮编译 · 累计已受理 {job.native_compile_history?.length ?? 0} 次 Compile
                {(job.native_repairs?.length ?? 0) > 0 && ` · 其中 ${job.native_repairs?.length} 次修订`}
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {job.submission_seconds !== undefined && (
                <TimingCard label="启动准备合计" seconds={job.submission_seconds} />
              )}
              {compileSeconds !== null && (
                <TimingCard label={job.native_explicit_retry ? "本轮经过时间（含中断等待）"
                  : job.finished_at ? "OV Compile" : "OV Compile（累计）"} seconds={compileSeconds} />
              )}
              {nativeRetrySeconds !== null && (
                <TimingCard label="本次重试经过时间" seconds={nativeRetrySeconds} />
              )}
              {job.materialize_seconds !== undefined && (
                <TimingCard label="产物物化" seconds={job.materialize_seconds} />
              )}
            </div>
            {(job.ov_stage_history?.length || 0) > 0 && (
              <div className="mt-3 border-t border-line pt-3">
                <div className="mb-2 text-[10.5px] font-semibold text-muted-foreground">远端阶段观测</div>
                <div className="flex flex-wrap gap-2">
                  {job.ov_stage_history?.map((entry, index) => (
                    <span key={`${entry.stage}-${index}`} className="rounded-md border border-border bg-surface-subtle px-2 py-1 text-[10px]">
                      <span className="font-semibold">{entry.stage}</span>
                      <span className="ml-1 text-muted-foreground">
                        {formatDuration(elapsedSeconds(entry.first_seen_at, entry.last_seen_at) || 0)}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {(job.skipped_checkpoints?.length || 0) > 0 && (
          <div className="rounded-lg border border-line bg-surface-subtle px-4 py-3 text-[12px] text-muted-foreground">
            {job.skipped_checkpoints?.map((entry) => (
              <p key={entry.round}>第 {entry.round} 轮没有补证问题，已自动跳过人工补证。</p>
            ))}
          </div>
        )}

        {hasKnowledgeSupplement && (
          <div className="overflow-hidden rounded-xl border border-amber-300/70 bg-amber-50/80 shadow-sm">
            <div className={cn("flex flex-wrap items-center justify-between gap-3 px-4 py-3.5", knowledgeSupplementExpanded && "border-b border-amber-200")}>
              <div className="flex min-w-0 items-center gap-2.5">
                <ListChecks className="size-4 shrink-0 text-amber-700" />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[14px] font-bold">Evidence 补充与复核</span>
                    {job.pending_checkpoint
                      ? <Pill tone="amber">等待人工补证</Pill>
                      : supplementRounds.length > 0
                        ? <Pill tone="amber">{supplementRounds.length} 轮记录</Pill>
                        : <Pill tone="amber">{job.knowledge_gaps?.total || durableGaps.length} 项缺口</Pill>}
                    {job.pending_checkpoint && (
                      <span className="text-[11px] text-amber-800">
                        第 {job.pending_checkpoint.round} 轮 · {job.pending_checkpoint.questions.length} 个问题
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <Button
                size="sm"
                variant="outline"
                aria-expanded={knowledgeSupplementExpanded}
                onClick={() => setKnowledgeSupplementExpanded((expanded) => !expanded)}
              >
                {knowledgeSupplementExpanded ? "收起" : "展开"}
                <ChevronDown className={cn("size-3.5 transition-transform", knowledgeSupplementExpanded && "rotate-180")} />
              </Button>
            </div>

            {knowledgeSupplementExpanded && (
              <div className="p-4">
                {job.pending_checkpoint ? (
                  <>
                    {currentRemoteWarnings.length > 0 && (
                      <RemoteCompileWarnings warnings={currentRemoteWarnings} label="本轮 Compile 执行提示" />
                    )}
                    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="text-[14px] font-bold text-foreground">{job.pending_checkpoint.title}</div>
                        <p className="mt-1 max-w-4xl text-[11.5px] leading-relaxed text-muted-foreground">
                          {job.pending_checkpoint.intro}
                        </p>
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {Object.values(checkpointAnswers).filter((value) => value.trim()).length} / {job.pending_checkpoint.questions.length} 已填写
                      </div>
                    </div>

                    <div className="grid gap-3 lg:grid-cols-2">
                      {job.pending_checkpoint.questions.map((question, index) => (
                        <div key={question.qid} className="rounded-xl border border-amber-200/80 bg-background p-3.5">
                          <div className="mb-2 flex flex-wrap items-center gap-2">
                            <span className="grid size-6 place-items-center rounded-full bg-amber-500 text-[11px] font-bold text-white">{index + 1}</span>
                            {question.severity && <Pill tone={question.severity === "高" ? "red" : "amber"}>{question.severity}优先级</Pill>}
                            {question.dimension && <span className="text-[10.5px] text-muted-foreground">{question.dimension}</span>}
                          </div>
                          <div className="text-[13px] font-semibold leading-relaxed">{question.question}</div>
                          {question.required && <span className="mt-1 block text-[10px] font-semibold text-amber-800">必填：回答或明确标记暂时无法确认</span>}
                          {question.context && (
                            <div className="mt-2 rounded-lg border-l-2 border-accent/40 bg-accent-soft px-2.5 py-2 text-[10.5px] leading-relaxed text-muted-foreground">
                              {question.context}
                            </div>
                          )}
                          {question.source && <div className="mt-1.5 text-[9.5px] text-muted-soft">参考来源：{question.source}</div>}
                          <label className="mt-3 block">
                            <span className="mb-1.5 block text-[11px] font-semibold text-muted-foreground">
                              {question.field_label || "你的回答"}
                            </span>
                            {question.answer_type === "short_text" ? (
                              <Input
                                aria-label={`问题 ${index + 1} 的回答`}
                                value={checkpointAnswers[question.qid] || ""}
                                disabled={checkpointBusy}
                                placeholder={question.placeholder || "请输入具体答案"}
                                onChange={(event) => setCheckpointAnswers((current) => ({ ...current, [question.qid]: event.target.value }))}
                              />
                            ) : (
                              <textarea
                                aria-label={`问题 ${index + 1} 的回答`}
                                rows={3}
                                disabled={checkpointBusy}
                                value={checkpointAnswers[question.qid] || ""}
                                placeholder={question.placeholder || "请输入具体答案"}
                                onChange={(event) => setCheckpointAnswers((current) => ({ ...current, [question.qid]: event.target.value }))}
                                className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-[12px] leading-relaxed outline-none transition-colors focus:border-accent"
                              />
                            )}
                          </label>
                          <label className="mt-3 block">
                            <span className="mb-1.5 block text-[11px] font-semibold text-muted-foreground">Evidence 来源 URI（每行一个）</span>
                            <textarea
                              aria-label={`问题 ${index + 1} 的 Evidence 来源`}
                              rows={2}
                              disabled={checkpointBusy}
                              value={checkpointReferences[question.qid] || ""}
                              placeholder="viking://resources/..."
                              onChange={(event) => setCheckpointReferences((current) => ({ ...current, [question.qid]: event.target.value }))}
                              className="mono w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-[11px] outline-none focus:border-accent"
                            />
                          </label>
                          <label className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
                            <input
                              type="checkbox"
                              aria-label={`问题 ${index + 1} 暂时无法确认`}
                              disabled={checkpointBusy}
                              checked={Boolean(checkpointUnresolved[question.qid])}
                              onChange={(event) => setCheckpointUnresolved((current) => ({ ...current, [question.qid]: event.target.checked }))}
                            />
                            暂时无法确认，保留此证据缺口
                          </label>
                        </div>
                      ))}
                    </div>

                    <div className="mt-4 space-y-3 rounded-xl border border-amber-200/80 bg-background p-3.5">
                      <label className="block">
                        <span className="mb-1.5 block text-[11px] font-semibold text-muted-foreground">本轮复核意见</span>
                        <textarea
                          aria-label="本轮复核意见"
                          rows={3}
                          value={checkpointFeedback}
                          disabled={checkpointBusy}
                          onChange={(event) => setCheckpointFeedback(event.target.value)}
                          placeholder="说明需要修订的内容、原文冲突或补充 Evidence 的用途。"
                          className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-[12px] outline-none focus:border-accent"
                        />
                      </label>
                      <label className="block text-[11px] font-semibold text-muted-foreground">
                        补充 Evidence 文件
                        <input
                          aria-label="补充 Evidence 文件"
                          type="file"
                          multiple
                          disabled={checkpointBusy}
                          className="mt-2 block w-full text-[11px] font-normal"
                          onChange={(event) => {
                            const files = Array.from(event.target.files || []);
                            event.target.value = "";
                            void uploadCheckpointEvidence(job, files);
                          }}
                        />
                      </label>
                      {checkpointUploading && <p role="status" className="text-[11px] text-muted-foreground">正在准备补充 Evidence…</p>}
                      {checkpointFiles.length > 0 && (
                        <ul aria-label="已准备补充 Evidence" className="space-y-1 text-[11px]">
                          {checkpointFiles.map((file) => <li key={file.uri} className="break-all select-text">{file.name} · {file.uri}</li>)}
                        </ul>
                      )}
                    </div>

                    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-amber-200 pt-3">
                      <span className="text-[10.5px] text-muted-foreground">
                        {job.pending_checkpoint.round >= job.max_rounds
                          ? "已达到轮数上限。结束后保留最终产物及所有未解决缺口。"
                          : "人工等待不占运行并发，也不计入单轮运行期限；未填写的问题会保留为证据缺口。"}
                      </span>
                      <div className="flex flex-wrap gap-2">
                        <Button variant="outline" disabled={checkpointBusy} onClick={() => submitCheckpointAnswers(job, "finish")}>
                          {checkpointSubmitting ? "提交中…" : "结束挖掘并保留最终产物"}
                        </Button>
                        {job.pending_checkpoint.round < job.max_rounds && (
                          <Button disabled={checkpointBusy} onClick={() => submitCheckpointAnswers(job)}>
                            {checkpointSubmitting ? "提交中…" : `提交补证并开始第 ${job.pending_checkpoint.round + 1} 轮`}
                            <ArrowRight className="size-3.5" />
                          </Button>
                        )}
                      </div>
                    </div>
                    {supplementRounds.length > 0 && (
                      <div className="mt-5 border-t border-amber-200 pt-4">
                        <div className="mb-3 text-[12px] font-bold text-amber-900">已完成的补证记录</div>
                        <KnowledgeSupplementHistory rounds={supplementRounds} />
                      </div>
                    )}
                  </>
                ) : (
                  supplementRounds.length > 0 ? (
                    <KnowledgeSupplementHistory rounds={supplementRounds} />
                  ) : (
                  <div className="grid gap-3 lg:grid-cols-2">
                    {durableGaps.map((question, index) => (
                      <div key={`${question.qid}-${index}`} className="rounded-xl border border-amber-200/80 bg-background p-3.5">
                        <div className="mb-2 flex flex-wrap items-center gap-2">
                          <span className="grid size-6 place-items-center rounded-full bg-amber-500 text-[11px] font-bold text-white">{index + 1}</span>
                          {question.severity && <Pill tone={question.severity === "高" ? "red" : "amber"}>{question.severity}优先级</Pill>}
                          {question.field_label && <span className="text-[10.5px] font-semibold text-amber-800">{question.field_label}</span>}
                        </div>
                        <div className="text-[13px] font-semibold leading-relaxed">{question.question}</div>
                        {question.context && (
                          <div className="mt-2 rounded-lg border-l-2 border-accent/40 bg-accent-soft px-2.5 py-2 text-[10.5px] leading-relaxed text-muted-foreground">
                            {question.context}
                          </div>
                        )}
                        {question.source && <div className="mt-1.5 text-[9.5px] text-muted-soft">建议核对：{question.source}</div>}
                      </div>
                    ))}
                  </div>
                  )
                )}
              </div>
            )}
          </div>
        )}

        {job.artifact_quality && (
          <section aria-label="本轮复核提示" className={cn(
            "rounded-xl border p-4",
            job.artifact_quality.level === "ready"
              ? "border-success/35 bg-success/5"
              : job.artifact_quality.level === "incomplete"
                ? "border-destructive/30 bg-destructive/5"
                : "border-amber-300/70 bg-amber-50/80"
          )}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5">
                <ShieldAlert className={cn(
                  "size-4 shrink-0",
                  job.artifact_quality.level === "ready" ? "text-success" : job.artifact_quality.level === "incomplete" ? "text-destructive" : "text-amber-700"
                )} />
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-bold">产物质量 · 本轮复核提示</span>
                    <Pill tone={artifactQualityTone(job.artifact_quality.level)}>{qualityLabel(job.artifact_quality)}</Pill>
                    <span className="text-[11px] text-muted-foreground">{job.artifact_quality.warnings.length} 条提示 · 产物阶段：{job.artifact_quality.confidence === "unknown" ? "未声明" : job.artifact_quality.confidence}</span>
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{job.artifact_quality.summary}</p>
                </div>
              </div>
              {job.artifact_quality.level !== "ready" && (
                <span className="text-[10.5px] font-medium text-amber-800">提交进化前会再次确认</span>
              )}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">“需复核”表示仍有内容需要人工核对。结构校验通过仅确认产物格式，业务正确性还需 True Replay 和 Candidate Review 验证。</p>
            {job.artifact_quality.warnings.length > 0 && (
              <details className="mt-2 border-t border-current/10 pt-2">
                <summary className="cursor-pointer text-[11px] font-semibold">查看本轮复核提示（{job.artifact_quality.warnings.length} 条）</summary>
                <p className="mt-2 text-[11px] text-muted-foreground">以下是本轮整体提示，请结合各个 Skill 的产物核对。</p>
                <ul className="mt-2 space-y-1 text-[11px] leading-relaxed text-muted-foreground">
                  {job.artifact_quality.warnings.map((warning, index) => <li key={`${warning}-${index}`} className="select-text whitespace-pre-wrap break-words">• {warning}</li>)}
                </ul>
              </details>
            )}
          </section>
        )}

        {isExecutingJob(job.status) ? (
          <div className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-[minmax(360px,0.8fr)_minmax(0,1.2fr)]">
            <div className="rounded-xl border border-border bg-surface p-4">
              <div className="mb-4 flex items-center justify-between text-xs">
                <span className="font-semibold">挖掘流水线（仅表示阶段位置）</span>
                <span className="text-muted-foreground">
                  {job.status === "queued" || job.status === "preparing"
                    ? "等待调度"
                    : `第 ${job.current_round || 1} / ${job.max_rounds} 轮 · ${job.ov_stage || "等待远端阶段上报"}`}
                </span>
              </div>
              <div className="space-y-3">
                {STEP_META.map((step, index) => {
                  const stepState = job.phase?.[step.key] || "idle";
                  const Icon = step.icon;
                  return (
                    <div key={step.key} className="flex items-center gap-3">
                      <span className={cn(
                        "grid size-8 shrink-0 place-items-center rounded-lg text-xs font-bold",
                        stepState === "done" ? "bg-success text-white" : stepState === "active" ? "bg-accent text-white" : "bg-muted text-muted-foreground"
                      )}>
                        {stepState === "done" ? <CheckCircle2 className="size-4" /> : index + 1}
                      </span>
                      <Icon className="size-4 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="text-[13px] font-semibold">{step.title}</div>
                        <div className="text-[10px] text-muted-soft">{step.sub}</div>
                      </div>
                      {stepState === "active" && <Pill tone="blue">进行中</Pill>}
                      {stepState === "done" && <Pill tone="green">已完成</Pill>}
                    </div>
                  );
                })}
              </div>
              <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${jobProgress(job)}%` }}
                />
              </div>
            </div>
            <div className="rounded-xl border border-border bg-[#0f172a] p-3">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold text-[#cbd5e1]">
                <Clock3 className="size-3.5" /> 实时日志
              </div>
              <div className="mono max-h-64 min-h-40 overflow-auto whitespace-pre-wrap break-words text-[10.5px] leading-relaxed text-[#cbd5e1] select-text">
                {job.logs?.length
                  ? job.logs.map((line, index) => <div key={index}>{line}</div>)
                  : <span className="text-muted-soft">任务正在准备，暂无日志。</span>}
              </div>
            </div>
            </div>
            {artifactGroups.candidates.some((group) => group.artifacts.some((file) => file.kind === "skill")) && (
              <section aria-label="Skill 文件预览（生成中）" className="rounded-xl border border-border bg-surface p-4">
                <h3 className="text-[13px] font-bold">Skill 文件预览（生成中）</h3>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  已生成的正文可以先查看。交付内容及完成规则由本任务冻结的编译 Skill 决定。
                </p>
                <div className="mt-3 space-y-3">
                  {artifactGroups.candidates.filter((group) => group.artifacts.some((file) => file.kind === "skill"))
                    .map(({ skillName, artifacts }) => (
                      <div key={skillName} className="rounded-lg border border-border bg-background p-3">
                        <div className="mb-2 flex items-center gap-2">
                          <span className="text-[12px] font-semibold">{artifacts.find((file) => file.kind === "skill")?.display_title || job.capability_plan?.skills.find((skill) => skill.name === skillName)?.task || skillName}</span>
                          <Pill tone="blue">生成中 · 只读预览</Pill>
                        </div>
                        <p className="mb-2 text-[11px] text-muted-foreground select-text">标识：{skillName}</p>
                        <MiningArtifactFiles artifacts={artifacts} editable={false} loadingPath={loadingArtifact} onPreview={previewArtifact} />
                      </div>
                    ))}
                </div>
              </section>
            )}
          </div>
        ) : (
          <div className="rounded-xl border border-border bg-surface p-4">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div className="text-[13px] font-bold">第 {job.current_round || 1} 轮{job.status === "failed" && !jobArtifacts(job).length ? " · 未生成可审核产物" : "产物"}</div>
              <div className="flex items-center gap-2">
                {job.finished_at && <span className="text-[10px] text-muted-soft">{job.status === "succeeded" ? "完成于" : "结束于"} {formatDateTime(job.finished_at)}</span>}
                {job.status === "succeeded" && (
                  <Button
                    size="sm"
                    disabled={Boolean(submittingJobId) || !selectedNames.length}
                    onClick={() => submitJobToEvolution(job, selectedNames)}
                  >
                    <Send className="size-3.5" />
                    {submittingJobId === job.job_id ? "提交中…" : showCandidateGroups ? `提交所选候选（${selectedNames.length}）` : job.artifact_quality && job.artifact_quality.level !== "ready" ? "谨慎提交到进化" : "提交到进化"}
                  </Button>
                )}
              </div>
            </div>
            {job.status === "failed" && (
              <section aria-label="本轮 Compile 失败" className="mb-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-[12px] text-destructive">
                <p className="font-semibold">{compileIssues.reduceMergeTruncated
                  ? "文件冲突合并（reduce_merge）输出截断：模型输出达到单次长度上限。"
                  : compileIssues.reduceTruncated
                    ? "Reduce 输出截断：模型输出达到单次长度上限。"
                    : "本轮 Compile 执行失败。"}</p>
                {job.error && <p className="mt-1 select-text whitespace-pre-wrap break-words text-[11px]">{job.error}</p>}
              </section>
            )}
            {artifactGroups.candidates.length > 0 && (
              <section aria-label="Skill Candidate 独立提交" className="mb-3 space-y-2">
                {artifactGroups.candidates.map(({ skillName, artifacts }) => {
                  const quality = job.candidate_quality?.[skillName];
                  const capability = job.capability_plan?.skills.find((item) => item.name === skillName);
                  const displayTitle = artifacts.find((artifact) => artifact.kind === "skill")?.display_title || capability?.task || skillName;
                  // Display orphan files without extending the submission whitelist.
                  const canSubmit = eligibleNames.includes(skillName);
                  return (
                    <section key={skillName} aria-label={`Skill Candidate ${skillName}`} className="rounded-xl border border-border bg-background p-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <label className="flex items-center gap-2 text-[12px] font-semibold">
                          {job.status === "succeeded" && (
                            <input
                              type="checkbox"
                              aria-label={`选择 ${skillName}`}
                              checked={selectedNames.includes(skillName)}
                              disabled={Boolean(submittingJobId) || !canSubmit}
                              onChange={(event) => setCandidateSelections((previous) => ({
                                ...previous,
                                [job.job_id]: event.target.checked
                                  ? [...selectedNames, skillName]
                                  : selectedNames.filter((name) => name !== skillName),
                              }))}
                            />
                          )}
                          <span className="select-text">{displayTitle}</span>
                          {quality && <Pill tone={artifactQualityTone(quality.level)}>{qualityLabel(quality)}</Pill>}
                        </label>
                        {job.status === "succeeded" && (
                          <Button size="sm" variant="outline" disabled={Boolean(submittingJobId) || !canSubmit} onClick={() => submitJobToEvolution(job, [skillName])} aria-label={`提交 ${skillName} 到进化`}>
                            <Send className="size-3.5" /> 单独提交
                          </Button>
                        )}
                      </div>
                      {displayTitle !== skillName && <p className="mt-1 text-[11px] text-muted-foreground select-text">标识：{skillName}</p>}
                      {quality && <p className="mt-1 text-[11px] text-muted-foreground select-text">{quality.summary}</p>}
                      {job.artifact_quality?.warnings.length ? <p className="mt-1 text-[11px] text-amber-700">本轮有 {job.artifact_quality.warnings.length} 条公共提示，请查看“本轮复核提示”。</p> : null}
                      {quality?.warnings.length ? (
                        <details className="mt-2 text-[11px]">
                          <summary className="cursor-pointer font-semibold text-amber-700">查看该候选的问题（{quality.warnings.length} 条）</summary>
                          <ul className="mt-1 space-y-1 text-muted-foreground">{quality.warnings.map((warning, index) => <li key={index} className="select-text whitespace-pre-wrap break-words">{warning}</li>)}</ul>
                        </details>
                      ) : null}
                      {capability && <CapabilityDetails capability={capability} />}
                      <div className="mt-3 border-t border-border pt-3">
                        <MiningArtifactFiles artifacts={artifacts} editable={editableArtifacts} loadingPath={loadingArtifact} onPreview={previewArtifact} />
                        {!artifacts.length && <p className="text-[11px] text-muted-foreground">该候选尚无可预览的配套文件，请补齐产物后再提交。</p>}
                      </div>
                    </section>
                  );
                })}
              </section>
            )}
            {candidateReport.length > 0 && (
              <section aria-label="Skill Candidate 提交结果" aria-live="polite" className="mb-3 rounded-lg border border-border bg-background p-3">
                <p className="text-[12px] font-semibold">提交结果：{candidateReport.filter((item) => item.state !== "failed").length} 个成功，{candidateReport.filter((item) => item.state === "failed").length} 个失败</p>
                <ul className="mt-2 space-y-1 text-[11px]">
                  {candidateReport.map((item) => <li key={item.skillName} className={cn("select-text", item.state === "failed" && "text-destructive")}>{item.skillName}：{item.state === "created" ? "已生成 Skill Candidate" : item.state === "existing" ? "当前产物版本已在候选区中" : `提交失败 · ${item.error}`}</li>)}
                </ul>
                <div className="mt-2 flex flex-wrap gap-2">
                  {failedNames.length > 0 && <Button size="sm" variant="outline" disabled={Boolean(submittingJobId)} onClick={() => submitJobToEvolution(job, failedNames)}>重试失败候选（{failedNames.length}）</Button>}
                  {candidateReport.some((item) => item.state !== "failed") && onNavigate && <Button size="sm" variant="outline" onClick={() => onNavigate("candidates")}>查看候选区 <ArrowRight className="size-3.5" /></Button>}
                </div>
              </section>
            )}
            {!jobArtifacts(job).length && (
              <div className="select-text whitespace-pre-wrap break-words rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted-soft">
                {job.status === "failed" ? "本轮未生成可审核产物。" : job.error || job.stop_reason || "该任务没有产生可预览产物。"}
              </div>
            )}
          </div>
        )}

        {(job.capability_plan || artifactGroups.shared.length > 0) && (
          <section aria-label="本轮共享资料" className="rounded-xl border border-border bg-background p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-bold">本轮共享资料</span>
              <Pill tone="gray">{artifactGroups.shared.length} 个文件</Pill>
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">这里汇总本轮的来源、Evidence 缺口和拆分说明，供所有候选共同参考。</p>
            {job.capability_plan && (
              <section aria-label="能力拆分计划" className="mt-3 rounded-lg border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[12px] font-semibold">能力拆分计划</span>
                  <Pill tone="purple">{job.capability_plan.skills.length} 个 Skill Candidate</Pill>
                </div>
                <p className="mt-1 text-[11px] text-muted-foreground">将来源中的流程按可独立完成的任务整理成候选；每个候选的说明与配套文件展示在同一组，分别验证和评审。</p>
                {job.capability_plan.skills.length === 0 && <p className="mt-2 text-[12px] text-muted-foreground">本轮没有形成 Skill Candidate，可查看共享分析了解原因。</p>}
                {job.capability_plan.deferred.length > 0 && (
                  <details className="mt-2 border-t border-border pt-2">
                    <summary className="cursor-pointer text-[12px] font-semibold">暂缓形成 Skill Candidate 的内容（{job.capability_plan.deferred.length} 项）</summary>
                    <p className="mt-2 text-[11px] text-muted-foreground">这些内容还缺少能支持完整任务的 Evidence，暂未形成候选；补充依据后可再挖掘。</p>
                    <ul className="mt-2 space-y-2 text-[11px] text-muted-foreground">
                      {job.capability_plan.deferred.map((item, index) => (
                        <li key={index} className="select-text whitespace-pre-wrap break-words">
                          <p>{item.reason}</p>
                          {item.source_refs.length > 0 && <p className="mono mt-1 text-[10px]">{item.source_refs.join("\n")}</p>}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </section>
            )}
            <div className="mt-3">
              <MiningArtifactFiles artifacts={artifactGroups.shared} editable={editableArtifacts} loadingPath={loadingArtifact} onPreview={previewArtifact} />
            </div>
          </section>
        )}

        {Boolean(job.rounds?.length) && (
          <section className="rounded-xl border border-border bg-background p-4" aria-label="挖掘轮次记录">
            <div className="mb-3 text-[13px] font-bold">轮次记录</div>
            <div className="space-y-3">
              {job.rounds!.map((round) => (
                <details key={round.round} className="rounded-lg border border-border p-3">
                  <summary className="cursor-pointer text-[12px] font-semibold">
                    第 {round.round} 轮 · {round.artifacts?.length || 0} 个产物
                    {round.round === job.current_round ? " · 当前轮次" : " · 已归档"}
                    {distinctWarnings(round.remote_warnings).length > 0 ? " · Compile 执行有提示" : ""}
                  </summary>
                  <div className="mt-2 space-y-2 text-[11px] text-muted-foreground">
                    {distinctWarnings(round.remote_warnings).length > 0 && (
                      <RemoteCompileWarnings warnings={distinctWarnings(round.remote_warnings)} label={`第 ${round.round} 轮 Compile 执行提示`} />
                    )}
                    {(round.ov_task_id || round.task_id) && <div className="mono break-all select-text">Compile：{round.ov_task_id || round.task_id}</div>}
                    {round.started_at && <div>开始于 {formatDateTime(round.started_at)}{round.finished_at ? ` · 完成于 ${formatDateTime(round.finished_at)}` : ""}</div>}
                    {round.round !== job.current_round && <ArchivedArtifactGroups artifacts={round.artifacts || []} round={round.round} loadingPath={loadingArtifact} onPreview={previewArtifact} />}
                    {round.round === job.current_round && <p>当前产物可在上方审核；等待人工补证或任务结束时可编辑。</p>}
                  </div>
                </details>
              ))}
            </div>
          </section>
        )}
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title={meta.title}
        description={meta.description}
        badge="SkillMiner"
        actions={running && <Pill tone="blue">{jobCounts.running ? `${jobCounts.running} 个运行中` : `${jobCounts.queued} 个排队中`}</Pill>}
      />

      {/* ---- 总览 ---- */}
      {page === "overview" && (
        <div className="px-[22px] py-[22px]">
          <div className="mb-[18px] grid grid-cols-2 gap-4 md:grid-cols-4">
            <StatCard label="知识源" value={config ? String(inputSources.length) : "—"} />
            <StatCard label="文档总数" value={config ? hasUnverifiedSources ? "待检查" : String(sourceDocumentTotal) : "—"} />
            <StatCard label="总任务" value={config ? String(jobCounts.total) : "—"} />
            <StatCard label="任务状态" value={stateLabel} />
          </div>

          <Panel
            title="任务流水线"
            count={pipelineJobs.length ? `${pipelineJobs.length} 个活动任务` : undefined}
            extra={<Pill tone={stateTone as any}>{stateLabel}</Pill>}
          >
            {pipelineJobs.length === 0 ? (
              <div className="p-8 text-center text-sm text-muted-soft">
                {jobCounts.queued ? `${jobCounts.queued} 个任务等待调度` : "当前没有运行中的挖掘任务。"}
              </div>
            ) : (
              <div className="max-h-[440px] space-y-3 overflow-auto p-4">
                {pipelineJobs.map((job) => (
                  <div key={job.job_id} className="rounded-lg border border-border bg-background p-3.5">
                    <div className="mb-3 flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <button
                          type="button"
                          className="block max-w-full truncate text-left text-[13px] font-bold hover:text-accent select-text"
                          onClick={() => {
                            setSelectedJobId(job.job_id);
                            setSelectedJob(job);
                            onNavigate?.("jobs");
                          }}
                        >
                          {job.name}
                        </button>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10.5px] text-muted-foreground">
                          <span>{job.input_dir}</span>
                          <span>第 {Math.max(1, job.current_round || 1)} / {job.max_rounds} 轮</span>
                        </div>
                      </div>
                      <Pill tone={jobStatusMeta(job.status).tone as any}>{jobStatusMeta(job.status).label}</Pill>
                    </div>
                    <div className="grid gap-2 sm:grid-cols-3">
                      {STEP_META.map((step) => {
                        const stepState = job.phase?.[step.key] || "idle";
                        return (
                          <div
                            key={step.key}
                            className={cn(
                              "flex items-center gap-2 rounded-md border px-2.5 py-2",
                              stepState === "active"
                                ? "border-accent/50 bg-accent-soft"
                                : stepState === "done"
                                  ? "border-success/30 bg-success/5"
                                  : "border-border bg-surface-subtle"
                            )}
                          >
                            <span className={cn(
                              "grid size-5 shrink-0 place-items-center rounded text-[10px] font-bold text-white",
                              stepState === "idle" ? "bg-muted-soft" : "bg-accent"
                            )}>
                              {step.n}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-[11px] font-semibold">{step.title}</span>
                            <span className={cn(
                              "shrink-0 text-[10px]",
                              stepState === "active" ? "text-accent" : stepState === "done" ? "text-success" : "text-muted-soft"
                            )}>
                              {pipelineStepLabel(stepState)}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
                {jobCounts.queued > 0 && (
                  <div className="rounded-lg border border-dashed border-border px-3 py-2 text-center text-[11px] text-muted-foreground">
                    另有 {jobCounts.queued} 个任务等待调度
                  </div>
                )}
              </div>
            )}
          </Panel>

          <Panel title="近期任务">
            {mining.jobs.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted-soft">暂无挖掘任务。</div>
            ) : (
              <ul className="divide-y divide-line">
                {mining.jobs.slice(0, recentTaskCount).map((job) => (
                  <li key={job.job_id} className="flex items-center justify-between gap-3 px-4 py-3 text-[13px]">
                    <button
                      type="button"
                      className="min-w-0 flex-1 truncate text-left font-semibold hover:text-accent select-text"
                      onClick={() => {
                        setSelectedJobId(job.job_id);
                        setSelectedJob(job);
                        onNavigate?.("jobs");
                      }}
                    >
                      {job.name}
                    </button>
                    <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:block">
                      {job.input_dir}
                    </span>
                    <Pill tone={jobStatusMeta(job.status).tone as any}>{jobStatusMeta(job.status).label}</Pill>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <MiningWhiteboxPanel active={active} user={_user} />
        </div>
      )}

      {/* ---- 知识源 ---- */}
      {page === "sources" && (
        <SkillSourceWorkspace
          sources={inputSources}
          selectedPath={inputDir}
          onSelect={selectInputDir}
          onRefresh={() => mining.refreshConfig(false)}
        />
      )}

      {/* ---- 挖掘任务 ---- */}
      {page === "jobs" && (
        <div className="space-y-[18px] px-[22px] py-[22px]">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-6">
            <StatCard label="全部任务" value={String(jobCounts.total)} />
            <StatCard label="运行中" value={String(jobCounts.running)} />
            <StatCard label="排队中" value={String(jobCounts.queued)} />
            <StatCard label="等待补证" value={String(jobCounts.waiting)} />
            <StatCard label="已完成" value={String(jobCounts.completed)} />
            <StatCard label="异常 / 停止" value={String(jobCounts.failed)} />
          </div>

          <Panel
            title="Skill 挖掘任务"
            count={filteredJobs.length ? `${filteredJobs.length} 个` : undefined}
            extra={
              <div className="flex items-center gap-2">
                <select
                  value={jobFilter}
                  onChange={(event) => setJobFilter(event.target.value as typeof jobFilter)}
                  className="rounded-lg border border-border bg-background px-3 py-1.5 text-xs outline-none focus:border-accent"
                >
                  <option value="all">全部任务</option>
                  <option value="active">进行中</option>
                  <option value="completed">已完成</option>
                  <option value="failed">异常 / 已停止</option>
                </select>
                <Button size="sm" onClick={() => setCreateJobOpen(true)}>
                  <FolderPlus className="size-3.5" /> 新建 Skill 挖掘
                </Button>
              </div>
            }
          >
            {mining.jobs.length === 0 ? (
              <div className="flex flex-col items-center gap-3 p-10 text-center text-sm text-muted-soft">
                <ListChecks className="size-8" />
                <span>暂无挖掘任务。</span>
                <Button size="sm" onClick={() => setCreateJobOpen(true)}>创建第一个任务</Button>
              </div>
            ) : (
              <div className="min-w-0">
                <div className="grid grid-cols-[112px_minmax(180px,1fr)_minmax(120px,0.65fr)_150px_28px] border-b border-line bg-surface-subtle px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  <span>状态</span><span>任务</span><span>知识源</span><span>创建时间</span><span />
                </div>
                {filteredJobs.length === 0 ? (
                  <div className="p-8 text-center text-sm text-muted-soft">当前筛选下没有任务。</div>
                ) : filteredJobs.map((job) => {
                  const status = jobStatusMeta(job.status);
                  const expanded = selectedJobId === job.job_id;
                  return (
                    <div key={job.job_id} className="border-b border-line last:border-b-0">
                      <button
                        type="button"
                        aria-expanded={expanded}
                        onClick={() => {
                          if (expanded) {
                            setSelectedJobId("");
                            setSelectedJob(null);
                          } else {
                            setSelectedJobId(job.job_id);
                            setSelectedJob(null);
                          }
                        }}
                        className={cn(
                          "grid w-full grid-cols-[112px_minmax(180px,1fr)_minmax(120px,0.65fr)_150px_28px] items-center px-4 py-3 text-left text-[13px] transition-colors hover:bg-surface-subtle",
                          expanded && "bg-accent-soft"
                        )}
                      >
                        <span className="flex flex-col items-start gap-1">
                          <Pill tone={status.tone as any}>{status.label}</Pill>
                          {job.remote_state_stale && <span className="text-[9px] font-semibold text-amber-700">远端待同步</span>}
                        </span>
                        <span className="min-w-0 pr-4">
                          <span className="block truncate font-semibold select-text">{job.name}</span>
                          <span className="mono mt-0.5 block truncate text-[10px] text-muted-soft select-text">{job.job_id}</span>
                        </span>
                        <span className="mono truncate pr-3 text-[11px] text-muted-foreground select-text">
                          {job.input_dir ? job.input_dir : "历史归档"}
                        </span>
                        <span className="text-[11px] text-muted-foreground">{formatDateTime(job.created_at)}</span>
                        <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", expanded && "rotate-180 text-accent")} />
                      </button>
                      {expanded && (
                        selectedJob?.job_id === job.job_id
                          ? renderJobDetail(selectedJob)
                          : <div className="border-t border-accent/20 bg-accent-soft/35 px-5 py-8 text-center text-xs text-muted-soft">正在加载任务详情…</div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>

        </div>
      )}

      <Dialog open={createJobOpen} onOpenChange={(open) => !starting && setCreateJobOpen(open)}>
        <DialogContent className="max-h-[90vh] w-[calc(100vw-32px)] overflow-y-auto !max-w-[1120px]">
          <DialogHeader>
            <DialogTitle>新建 Skill 挖掘任务</DialogTitle>
          </DialogHeader>

          <div className="grid gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
            <div className="rounded-xl border border-border bg-surface-subtle p-4">
              <div className="mb-4 text-[13px] font-bold">任务配置</div>
              <div className="space-y-4">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">任务名称</span>
                  <Input
                    value={taskName}
                    onChange={(event) => setTaskName(event.target.value)}
                    placeholder="例如：智能客服技能挖掘"
                    maxLength={120}
                    autoFocus
                  />
                </label>

                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">挖掘模式</span>
                  <select aria-label="挖掘模式" value={miningMode} disabled={starting}
                    onChange={event => setMiningMode(event.target.value as typeof miningMode)}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px]">
                    <option value="auto">自由挖掘</option>
                    <option value="single">单 Skill 整合</option>
                    <option value="target">指定数量</option>
                  </select>
                  <span className="mt-1 block text-[11px] text-muted-foreground">
                    {miningMode === "auto" ? "适合大量、主题未知的素材。根据来源决定独立方法及数量。"
                      : miningMode === "single" ? "适合为一个完整方法准备的知识源。子步骤与情境变体整合到同一 Skill。"
                      : "先识别完整独立方法，再优先选择目标数量；数量不作为拆分或合并依据。"}
                  </span>
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                    挖掘方向{miningMode === "single" ? "（必填）" : "（选填）"}
                  </span>
                  <textarea aria-label="挖掘方向" value={miningDirection} maxLength={4000} rows={4} disabled={starting}
                    onChange={event => setMiningDirection(event.target.value)}
                    placeholder="例如：挖掘从明确客户需求到确认下一步安排的完整客户沟通方法，人物和渠道作为参数。"
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px]" />
                  <span className="mt-1 block text-[11px] text-muted-foreground">
                    {miningMode === "single" ? "说明这一个 Skill 要完成的目标和适用边界。"
                      : "留空可探索全部素材；填写后聚焦指定方法、适用场景或排除范围。"}
                    方向不替代原文依据，也不允许补造业务事实。
                  </span>
                </label>
                {miningMode === "target" && <label className="block text-xs text-muted-foreground">
                  目标 Skill 数量
                  <input aria-label="目标 Skill 数量" type="number" min={1} max={100} step={1}
                    value={targetSkillCount} disabled={starting}
                    onChange={event => setTargetSkillCount(Number(event.target.value))}
                    className="ml-3 w-20 rounded border border-border bg-background px-2 py-1" />
                </label>}
                {miningMode !== "auto" && <p className="text-[11px] leading-relaxed text-muted-foreground">
                  {miningMode === "single" ? "目标为 1 个完整 Skill。" : "可设置 1–100 个。"}
                  来源不足时明确提示实际数量及缺口，等待复核；不会硬凑数量或强行拼接无关方法。
                </p>}

                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">选择知识源</span>
                  <select
                    aria-label="选择 Skill 知识源"
                    value={inputDir}
                    disabled={starting || !skillSources.length}
                    onChange={(event) => selectInputDir(event.target.value)}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                  >
                    {!skillSources.length && <option value="">暂无可用的 Skill 知识源</option>}
                    {skillSources.map((source) => (
                      <option key={source.path} value={source.path} disabled={source.status === "missing" || source.status === "over_limit"}>
                        {source.name || source.path.split("/").pop()}{source.ready ? "" : source.status === "unverified" ? "（待检查）" : `（${sourceStatusMeta(source).label}）`}
                      </option>
                    ))}
                  </select>
                  <div className="mt-2 flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={starting || checkingInputSource || !selectedSource?.source_id}
                      onClick={() => void verifySelectedInputSource()}
                    >
                      {checkingInputSource ? "检查中…" : "检查选中目录"}
                    </Button>
                    {selectedSource?.status === "unverified" && !checkingInputSource && (
                      <span className="text-[11px] text-muted-foreground">仅检查当前选择的目录</span>
                    )}
                  </div>
                  <div className={cn(
                    "mt-1.5 text-[11px]",
                    selectedSource?.ready ? "text-muted-foreground" : selectedSourceError || selectedSource?.status === "over_limit" ? "text-destructive" : "text-amber-700"
                  )}>
                    {!config
                      ? "正在检查目录…"
                      : selectedSource?.ready
                        ? `${selectedSource.path} · ${selectedSource.document_count} 个文档 · ${formatBytes(selectedSource.total_bytes)}`
                        : selectedSource?.status === "unverified" && !checkingInputSource
                          ? "尚未检查；创建任务前请先验证此来源。"
                          : checkingInputSource
                            ? "正在检查所选目录的完整文件清单…"
                        : selectedSource?.status === "over_limit"
                          ? `超过挖掘限额：${selectedSourceError || "单个文本文件不超过 20 MB，目录不超过 10000 个文件；大来源分批处理。"}`
                          : inputSourceCheckError || selectedSourceError
                          ? `知识源暂不可用：${inputSourceCheckError || selectedSourceError}`
                          : selectedSource?.status === "missing"
                            ? "OpenViking 中的目录已被删除，请重新选择知识源。"
                            : "该目录没有可挖掘文档，请先上传素材。"}
                  </div>
                </label>

                <label className="block">
                  <span className="mb-1.5 flex items-center justify-between text-xs font-semibold text-muted-foreground">
                    挖掘轮数上限
                    <span className="text-accent">{rounds}</span>
                  </span>
                  <input
                    type="range"
                    min={minRounds}
                    max={maxRounds}
                    value={rounds}
                    disabled={starting}
                    onChange={(event) => setRounds(Number(event.target.value))}
                    className="w-full accent-[var(--accent)]"
                  />
                </label>

                <label className="block text-xs text-muted-foreground">
                  单轮运行期限（秒，60–7200）
                  <input type="number" min={60} max={7200} value={runtimeTimeout} disabled={starting}
                    onChange={(event) => setRuntimeTimeout(Number(event.target.value))}
                    className="ml-3 rounded border border-border bg-background px-2 py-1" />
                  <span className="mt-1 block text-[11px]">每轮从远端受理后独立计时，包含远端排队；人工补证等待不计时。运行期限用于保护预算，不代表预计完成时间。到期请求取消并等待远端确认。</span>
                </label>

                <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border bg-background p-3.5 transition-colors hover:border-accent/50">
                  <input
                    type="checkbox"
                    checked={humanCheckpoints}
                    disabled={starting}
                    onChange={(event) => setHumanCheckpoints(event.target.checked)}
                    className="mt-0.5 size-4 accent-[var(--accent)]"
                  />
                  <span>
                    <span className="block text-[12px] font-semibold">有补证问题时等待人工补充 Evidence</span>
                    <span className="mt-1 block text-[11px] leading-relaxed text-muted-foreground">
                      默认整轮编译：有问题时可补充回答、引用和文档；问题数为 0 时直接结束。关闭后保留未解决问题，不发起没有新 Evidence 的轮次。
                    </span>
                  </span>
                </label>

                <div className="rounded-lg border border-border bg-background/70 p-3 text-[11px] leading-relaxed text-muted-foreground">
                  每轮编译使用 OpenViking 服务端的模型配置，并固化本轮输入。最多同时运行 {mining.jobSummary.max_parallel} 个任务；等待人工补证的任务不占运行并发，继续时需有可用名额。
                  <p className="mt-1">首轮按原始来源的主要语言冻结产物语言，中文主导用简体中文，后续轮沿用；字段名、ID、文件路径和来源 URI 保持原样。</p>
                </div>

                <div className="flex justify-end gap-2 pt-1">
                  <Button variant="outline" disabled={starting} onClick={() => setCreateJobOpen(false)}>取消</Button>
                  <Button onClick={startRun} disabled={starting || !selectedSource?.ready
                    || (miningMode === "single" && !miningDirection.trim())
                    || (miningMode === "target" && (!Number.isInteger(targetSkillCount)
                      || targetSkillCount < 1 || targetSkillCount > 100))}>
                    <Play className="size-4" /> {starting ? "创建中…" : "创建任务"}
                  </Button>
                </div>
              </div>
            </div>

            <div className="space-y-4">
              <div className="rounded-xl border border-border bg-surface p-4">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <div className="text-[13px] font-bold">挖掘流程预览</div>
                </div>

                <div className="grid gap-3 md:grid-cols-3">
                  {STEP_META.map((step) => {
                    const Icon = step.icon;
                    return (
                      <div key={step.key} className="rounded-xl border border-border bg-background p-3.5">
                        <div className="mb-2 flex items-center gap-2">
                          <span className="grid size-7 place-items-center rounded-lg bg-muted-soft text-[13px] font-extrabold text-white">{step.n}</span>
                          <Icon className="size-4 text-muted-foreground" />
                        </div>
                        <div className="text-[13px] font-bold">{step.title}</div>
                        <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{step.sub}</div>
                      </div>
                    );
                  })}
                </div>

                <div className="mt-4 flex items-center gap-2 rounded-lg border border-dashed border-border bg-surface-subtle px-3.5 py-2.5 text-[12px] font-semibold text-muted-foreground">
                  <RotateCcw className="size-4 text-accent" />
                  每轮一次整轮 Compile；需要补证时再增量修订，最多 {rounds} 轮
                </div>

                <div className="mt-4 flex items-center gap-3 rounded-lg border border-accent/30 bg-accent-soft px-4 py-3">
                  <Upload className="size-4 shrink-0 text-accent" />
                  <div className="text-[12.5px]">
                    <span className="font-bold text-accent">编译产物交付</span>
                    <span className="text-muted-foreground"> — 按编译 Skill 交付任务方法、配套资源与来源说明；新版不要求验证案例</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteJob} onOpenChange={(open) => !open && !jobDeleting && setDeleteJob(null)}>
        <DialogContent className="w-full !max-w-[480px]">
          <DialogHeader>
            <DialogTitle>删除挖掘任务</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm leading-relaxed text-muted-foreground">
              确定删除任务 <span className="font-semibold text-foreground">{deleteJob?.name || ""}</span>？
              此操作会永久删除该任务的输入快照、运行日志、Evidence 补充记录以及全部 Skill、语义报告和 Test Dataset 产物，不能恢复。
            </p>
            <p className="rounded-lg border border-amber-300/70 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-900">
              已提交到进化候选区的内容不会被删除；它们由进化模块独立管理。
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" disabled={jobDeleting} onClick={() => setDeleteJob(null)}>取消</Button>
              <Button disabled={jobDeleting} onClick={deleteMiningJob} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
                <Trash2 className="size-3.5" /> {jobDeleting ? "删除中…" : "确认删除任务"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!artifactPreview}
        onOpenChange={(open) => {
          if (!open && !artifactSaving) {
            if (artifactEditing && artifactDraft !== artifactPreview?.content) {
              const discard = window.confirm("当前文档有未保存修改，确定放弃修改并关闭吗？");
              if (!discard) return;
            }
            setArtifactPreview(null);
            setArtifactDraft("");
            setArtifactEditing(false);
            setArtifactEditable(false);
          }
        }}
      >
        <DialogContent
          onKeyDown={(event) => {
            if (!artifactEditing || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
            event.preventDefault();
            if (!artifactSaving && artifactDraft !== artifactPreview?.content) void saveArtifactRevision();
          }}
          className={cn(
            "flex w-full flex-col overflow-hidden p-0",
            artifactIsMarkdown ? "h-[92vh] !max-w-[1240px] gap-0" : "max-h-[88vh] !max-w-[980px] gap-4 p-4"
          )}
        >
          <DialogHeader className={cn("shrink-0", artifactIsMarkdown && "border-b border-border bg-surface px-5 py-3.5") }>
            <div className="flex items-center justify-between gap-3 pr-8">
              <div className="min-w-0">
                <DialogTitle className="truncate">
                  {artifactEditing ? "编辑产物" : "产物预览"} · {artifactPreview?.name}
                </DialogTitle>
                <div className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-soft">
                  <span className="mono truncate">{artifactPreview?.path}</span>
                  {artifactIsMarkdown && <span className="shrink-0 rounded-full bg-accent-soft px-2 py-0.5 font-semibold text-accent">Markdown</span>}
                </div>
              </div>
              {artifactEditable && !artifactEditing && (
                <Button size="sm" variant="outline" onClick={() => {
                  setArtifactEditorMode("edit");
                  setArtifactEditing(true);
                }}>
                  <Pencil className="size-3.5" /> 编辑产物
                </Button>
              )}
            </div>
          </DialogHeader>
          {artifactEditing ? (
            artifactIsMarkdown ? (
              <div className="flex min-h-0 flex-1 flex-col">
                <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-surface-subtle px-4 py-2">
                  <div className="flex rounded-lg border border-border bg-surface-subtle p-0.5" role="tablist" aria-label="Markdown 产物编辑模式">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={artifactEditorMode === "edit"}
                      className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", artifactEditorMode === "edit" && "bg-white shadow-sm")}
                      onClick={() => setArtifactEditorMode("edit")}
                    >
                      <Pencil className="size-3" /> 编辑
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={artifactEditorMode === "preview"}
                      className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", artifactEditorMode === "preview" && "bg-white shadow-sm")}
                      onClick={() => setArtifactEditorMode("preview")}
                    >
                      <Eye className="size-3" /> 预览
                    </button>
                  </div>
                  <span className="text-[11px] text-muted-foreground" role="status">
                    {artifactDraft !== artifactPreview?.content ? "未保存" : "已保存"}
                  </span>
                  <div className="ml-auto flex items-center gap-2">
                    <Button size="sm" variant="outline" disabled={artifactSaving} onClick={() => {
                      setArtifactDraft(artifactPreview?.content || "");
                      setArtifactEditing(false);
                    }}>
                      取消修改
                    </Button>
                    <Button size="sm" disabled={artifactSaving || artifactDraft === artifactPreview?.content} onClick={saveArtifactRevision} title="保存（⌘/Ctrl + S）">
                      <Save className="size-3.5" /> {artifactSaving ? "保存中…" : "保存修改"}
                    </Button>
                  </div>
                </div>
                {artifactEditorMode === "edit" ? (
                  <textarea
                    autoFocus
                    spellCheck={false}
                    disabled={artifactSaving}
                    value={artifactDraft}
                    aria-label="Markdown 正文编辑器"
                    onChange={(event) => setArtifactDraft(event.target.value)}
                    className="min-h-0 w-full flex-1 resize-none overflow-auto overscroll-contain border-0 bg-[#fbfcfe] p-5 font-mono text-xs leading-6 outline-none"
                  />
                ) : (
                  <div tabIndex={0} role="region" aria-label="Markdown 编辑预览" className="min-h-0 flex-1 overflow-auto overscroll-contain bg-white px-7 py-6">
                    <MarkdownDocument content={markdownBodyForPreview(artifactDraft)} />
                  </div>
                )}
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col gap-3">
                <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800">
                  该产物使用结构化源码格式。请保留 Test Dataset JSON 等文件的语法，提交进化时会再次执行完整性校验。
                </div>
                <textarea
                  autoFocus
                  spellCheck={false}
                  value={artifactDraft}
                  aria-label="产物源码编辑器"
                  onChange={(event) => setArtifactDraft(event.target.value)}
                  className="mono min-h-[420px] flex-1 resize-none overflow-auto rounded-lg border border-border bg-[#0f172a] p-4 text-[12px] leading-relaxed text-[#dbe4f0] outline-none focus:border-accent"
                />
                <div className="flex justify-end gap-2">
                  <Button
                    variant="outline"
                    disabled={artifactSaving}
                    onClick={() => {
                      setArtifactDraft(artifactPreview?.content || "");
                      setArtifactEditing(false);
                    }}
                  >
                    取消修改
                  </Button>
                  <Button
                    disabled={artifactSaving || artifactDraft === artifactPreview?.content}
                    onClick={saveArtifactRevision}
                  >
                    <Save className="size-3.5" /> {artifactSaving ? "保存中…" : "保存修改"}
                  </Button>
                </div>
              </div>
            )
          ) : (
            artifactIsMarkdown ? (
              <div tabIndex={0} role="region" aria-label="Markdown 产物内容" className="min-h-0 flex-1 overflow-auto overscroll-contain bg-[#f6f7f9] px-5 py-8">
                <div className="mx-auto min-h-full max-w-[820px] bg-white px-12 py-10 shadow-[0_1px_2px_rgba(15,23,42,0.04),0_14px_40px_rgba(15,23,42,0.06)]">
                  <MarkdownDocument content={markdownBodyForPreview(artifactPreview?.content || "")} />
                </div>
              </div>
            ) : (
              <pre tabIndex={0} role="region" aria-label="产物源码内容" className="mono min-h-0 flex-1 overflow-auto overscroll-contain whitespace-pre-wrap break-words rounded-lg bg-[#0f172a] p-4 text-[12px] leading-relaxed text-[#dbe4f0]">
                {artifactPreview?.content}
              </pre>
            )
          )}
        </DialogContent>
      </Dialog>

    </div>
  );
}

function TimingCard({ label, seconds, failed = false }: { label: string; seconds: number; failed?: boolean }) {
  return (
    <div className={cn("rounded-lg border px-3 py-2.5", failed ? "border-destructive/30 bg-destructive/5" : "border-border bg-surface-subtle") }>
      <div className="text-[10px] font-semibold text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 mono text-[14px] font-bold", failed && "text-destructive")}>{formatDuration(seconds)}</div>
    </div>
  );
}

function elapsedSeconds(startedAt?: string, finishedAt?: string) {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  const finish = finishedAt ? Date.parse(finishedAt) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return null;
  return Math.max(0, (finish - start) / 1000);
}

function nativeRetryContext(job: MiningJob) {
  const context = job.native_retry_context;
  return job.can_retry_native && context?.expected_task_id && context.format_guidance_sha256
    && Number.isFinite(context.budget_seconds) && context.budget_seconds > 0 ? context : null;
}

function formatDuration(seconds: number) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 1) return `${value.toFixed(3)} 秒`;
  if (value < 60) return `${value.toFixed(1)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`;
}

function isActiveJob(status: MiningJobStatus) {
  return isExecutingJob(status) || status === "waiting_human";
}

function isExecutingJob(status: MiningJobStatus) {
  return status === "preparing" || status === "queued" || status === "running" || status === "stopping";
}

function isWaitingForEvidence(job: MiningJob) {
  return job.status === "waiting_human";
}

function jobStatusMeta(status: MiningJobStatus) {
  const entries: Record<MiningJobStatus, { label: string; tone: string }> = {
    preparing: { label: "准备中", tone: "gray" },
    queued: { label: "排队中", tone: "blue" },
    running: { label: "运行中", tone: "blue" },
    waiting_human: { label: "等待人工补证", tone: "amber" },
    stopping: { label: "停止中", tone: "amber" },
    stopped: { label: "已停止", tone: "gray" },
    succeeded: { label: "已完成", tone: "green" },
    failed: { label: "失败", tone: "red" },
    interrupted: { label: "已中断", tone: "red" },
  };
  return entries[status] || entries.failed;
}

function jobProgress(job: MiningJob) {
  if (job.status === "queued" || job.status === "preparing") return 0;
  if (job.status === "succeeded") return 100;
  const completedRounds = Math.max(0, (job.current_round || 1) - 1);
  const doneSteps = STEP_META.filter((step) => job.phase?.[step.key] === "done").length;
  const activeStep = STEP_META.some((step) => job.phase?.[step.key] === "active") ? 0.5 : 0;
  return Math.min(98, Math.round(((completedRounds + (doneSteps + activeStep) / 3) / Math.max(1, job.max_rounds)) * 100));
}

function pipelineStepLabel(state: PhaseState[keyof PhaseState]) {
  if (state === "active") return "进行中";
  if (state === "done") return "完成";
  return "未开始";
}

function sourceStatusMeta(source: InputSource): { label: string; tone: PillTone } {
  if (source.status === "over_limit") return { label: "超过挖掘限额", tone: "red" };
  if (source.status === "missing") return { label: "路径已删除", tone: "red" };
  if (source.status === "unavailable") return { label: "暂不可用", tone: "amber" };
  if (source.ready) return { label: "可挖掘", tone: "green" };
  return { label: "空目录", tone: "amber" };
}

function jobArtifacts(job: MiningJob) {
  const source = job.artifacts
    ?? (job.rounds || []).find((round) => round.round === job.current_round)?.artifacts
    ?? [];
  const seen = new Set<string>();
  return source.filter((artifact) => {
    if (seen.has(artifact.path)) return false;
    seen.add(artifact.path);
    return true;
  });
}

function groupMiningArtifacts(artifacts: MiningArtifact[], preferredNames: string[] = []) {
  const candidates = new Map<string, MiningArtifact[]>(preferredNames.map((name) => [name, []]));
  const shared: MiningArtifact[] = [];
  const seen = new Set<string>();
  for (const artifact of artifacts) {
    if (seen.has(artifact.path)) continue;
    seen.add(artifact.path);
    if (artifact.skill_name) {
      if (!candidates.has(artifact.skill_name)) candidates.set(artifact.skill_name, []);
      candidates.get(artifact.skill_name)!.push(artifact);
    } else {
      shared.push(artifact);
    }
  }
  const order = ["SKILL.md", "EVALUATION.md", "benchmark.json", "BENCHMARK.md"];
  const rank = (artifact: MiningArtifact) => {
    const index = order.indexOf(artifact.name);
    return index === -1 ? order.length : index;
  };
  return {
    candidates: Array.from(candidates, ([skillName, files]) => ({ skillName, artifacts: files.sort((a, b) => rank(a) - rank(b)) })),
    shared,
  };
}

function qualityLabel(quality: MiningArtifactQuality) {
  return quality.level === "ready" ? "结构校验通过" : quality.label;
}

function CapabilityDetails({ capability }: { capability: MiningCapability }) {
  return (
    <details className="mt-2 text-[11px]">
      <summary className="cursor-pointer font-semibold text-muted-foreground">任务边界与拆分依据</summary>
      <dl className="mt-2 grid gap-x-3 gap-y-1 sm:grid-cols-[5rem_1fr]">
        {([
          ["触发场景", capability.trigger],
          ["输入", capability.inputs.join("；")],
          ["输出", capability.outputs.join("；")],
          ["能力边界", capability.boundary],
          ["拆分依据", capability.rationale],
          ["Evidence 来源", capability.source_refs.join("\n")],
        ] as const).map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="font-medium text-muted-foreground">{label}</dt>
            <dd className="select-text whitespace-pre-wrap break-words">{value || "未声明"}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function MiningArtifactFiles({ artifacts, editable, historical = false, loadingPath, onPreview }: {
  artifacts: MiningArtifact[];
  editable: boolean;
  historical?: boolean;
  loadingPath: string;
  onPreview: (artifact: MiningArtifact, editable: boolean) => void;
}) {
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {artifacts.map((artifact) => {
        const readOnly = historical || artifact.kind === "resource";
        const canEdit = editable && !readOnly;
        return (
          <button
            key={artifact.path}
            type="button"
            aria-label={`${artifact.skill_name ? `${artifact.skill_name} / ` : ""}${artifact.name}${readOnly ? " 只读" : ""}`}
            disabled={loadingPath === artifact.path}
            onClick={() => onPreview(artifact, canEdit)}
            className="flex w-full items-center gap-2 rounded-lg border border-border bg-background px-3 py-2.5 text-left transition-colors hover:border-accent"
          >
            <FileText className="size-4 shrink-0 text-accent" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12px] font-semibold select-text">{artifact.name}</span>
              <span className="mono block truncate text-[9.5px] text-muted-soft select-text">{artifact.path}</span>
            </span>
            <Pill tone={artifactTone(artifact.kind)}>{artifactLabel(artifact.kind)}</Pill>
            {readOnly && <span className="text-[10px]">只读</span>}
            {canEdit ? <Pencil className="size-3.5 text-muted-foreground" /> : <Eye className="size-3.5 text-muted-foreground" />}
          </button>
        );
      })}
    </div>
  );
}

function ArchivedArtifactGroups({ artifacts, round, loadingPath, onPreview }: {
  artifacts: MiningArtifact[];
  round: number;
  loadingPath: string;
  onPreview: (artifact: MiningArtifact, editable: boolean) => void;
}) {
  const groups = groupMiningArtifacts(artifacts);
  return (
    <div className="space-y-3">
      {groups.candidates.map(({ skillName, artifacts: files }) => (
        <section key={skillName} aria-label={`第 ${round} 轮 Skill Candidate ${skillName}`} className="rounded-lg border border-border p-3">
          <div className="mb-2 text-[12px] font-semibold select-text">{files.find((file) => file.kind === "skill")?.display_title || skillName} · 配套文件</div>
          <MiningArtifactFiles artifacts={files} editable={false} historical loadingPath={loadingPath} onPreview={onPreview} />
        </section>
      ))}
      {groups.shared.length > 0 && (
        <section aria-label={`第 ${round} 轮共享资料`} className="rounded-lg border border-border p-3">
          <div className="mb-2 text-[12px] font-semibold">本轮共享资料</div>
          <MiningArtifactFiles artifacts={groups.shared} editable={false} historical loadingPath={loadingPath} onPreview={onPreview} />
        </section>
      )}
    </div>
  );
}

function jobSkillNames(job: MiningJob) {
  return Array.from(new Set(
    [
      ...jobArtifacts(job)
        .filter((artifact) => artifact.kind === "skill" && artifact.skill_name)
        .map((artifact) => artifact.skill_name),
      ...(job.candidate_contract_version === 2 ? job.capability_plan?.skills.map((skill) => skill.name) || [] : []),
    ]
  ));
}

function candidateCanSubmit(job: MiningJob, skillName: string) {
  const quality = job.candidate_quality?.[skillName];
  const candidateReady = job.candidate_contract_version === 2
    ? quality?.can_submit === true && quality.has_skill
    : quality?.can_submit !== false;
  return job.status === "succeeded"
    && job.artifact_quality?.can_submit !== false
    && candidateReady;
}

function artifactTone(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "green";
  if (kind === "skill") return "purple";
  if (kind === "semantic") return "blue";
  return "gray";
}

function artifactQualityTone(state: MiningArtifactQuality["level"] | MiningArtifactQualityItem["state"]): PillTone {
  if (state === "ready") return "green";
  if (state === "incomplete") return "red";
  return "amber";
}

function artifactLabel(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "Test Dataset";
  if (kind === "skill") return "Skill";
  if (kind === "semantic") return "语义报告";
  if (kind === "evaluation") return "评测定义";
  if (kind === "resource") return "配套资源";
  return "文件";
}

function distinctWarnings(warnings?: string[]) {
  return Array.from(new Set((warnings || []).filter((warning) => typeof warning === "string" && warning.trim())));
}

function pipelineWarningMessages(errors: unknown): string[] {
  const values = Array.isArray(errors) ? errors.slice(0, 20) : [errors];
  return distinctWarnings(values.map((value) => {
    if (typeof value === "string") return value.slice(0, 4000);
    if (!value || typeof value !== "object") return "";
    const error = value as Record<string, unknown>;
    return ["stage", "code", "message", "error"].map((key) => error[key]).filter((part) => typeof part === "string").join(" · ").slice(0, 4000);
  }));
}

function compileIssueKinds(messages: string[]) {
  return {
    reduceMergeTruncated: messages.some((message) => /\breduce_merge\b\s*(?::|·)[\s\S]{0,300}(?:truncat|output\s+limit\s+reached)/i.test(message)),
    reduceTruncated: messages.some((message) => /\breduce\b\s*(?::|·|output\b)[\s\S]{0,300}(?:truncat|output\s+limit\s+reached)/i.test(message)),
    sourceIncomplete: messages.some((message) => /\bextract\s*(?::|·)|\bextract(?:ion)?(?:\s+(?:worker|job|step|stage))?\s+(?:failed|failure|incomplete)\b|\bsources?\s+(?:(?:processing|reading|extraction)\s+)?(?:failed|incomplete)\b|\bfailed\s+to\s+(?:read|process|extract)\s+(?:the\s+)?source\b|来源(?:读取|处理)?(?:失败|未完整处理)/i.test(message)),
  };
}

function RemoteCompileWarnings({ warnings, label, historical = false }: { warnings: string[]; label: string; historical?: boolean }) {
  const issues = compileIssueKinds(warnings);
  return (
    <section aria-label={label} className="mb-4 rounded-xl border border-amber-300/70 bg-amber-50 px-4 py-3 text-amber-900">
      <div className="flex items-center gap-2 text-[12px] font-bold"><ShieldAlert className="size-4 shrink-0" />{label}</div>
      {issues.reduceMergeTruncated && <p className="mt-1 text-[11px] leading-relaxed">文件冲突合并（reduce_merge）的输出达到单次长度上限，被截断。</p>}
      {issues.reduceTruncated && <p className="mt-1 text-[11px] leading-relaxed">Reduce 输出达到单次长度上限，被截断。</p>}
      {issues.sourceIncomplete && <p className="mt-1 text-[11px] leading-relaxed">{historical
        ? "挖掘期间部分来源曾未完整处理。请核对已有产物与各轮 Evidence 的覆盖情况。"
        : "部分来源未完整处理；请核对已有产物和缺失的 Evidence 后再决定继续或结束。"}</p>}
      {!issues.reduceMergeTruncated && !issues.reduceTruncated && !issues.sourceIncomplete && <p className="mt-1 text-[11px] leading-relaxed">Compile 执行出现提示，请结合任务状态和已有产物核对下方远端详情。</p>}
      <ul className="mt-2 space-y-1 text-[11px] leading-relaxed">
        {warnings.map((warning, index) => <li key={`${index}-${warning}`} className="select-text whitespace-pre-wrap break-words">{warning}</li>)}
      </ul>
    </section>
  );
}

function KnowledgeSupplementHistory({ rounds }: { rounds: KnowledgeSupplementRound[] }) {
  return (
    <div className="space-y-3">
      {rounds.map((record, recordIndex) => (
        <section key={`${record.round}-${record.checkpoint}-${recordIndex}`} className="overflow-hidden rounded-xl border border-amber-200/80 bg-background">
          <div className="flex flex-wrap items-start justify-between gap-2 border-b border-amber-100 bg-amber-50/70 px-3.5 py-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-bold">第 {record.round || 1} 轮 Evidence 补充</span>
                <Pill tone="green">已提交</Pill>
                <span className="text-[10.5px] text-muted-foreground">
                  本轮 Compile 后
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{record.title}</p>
              {record.intro && <p className="mt-1 text-[10px] leading-relaxed text-muted-soft">{record.intro}</p>}
            </div>
            {record.submitted_at && <span className="text-[10px] text-muted-soft">{formatDateTime(record.submitted_at)}</span>}
          </div>
          {record.feedback && <p className="mx-3.5 mt-3 select-text whitespace-pre-wrap text-[11px]">复核意见：{record.feedback}</p>}
          {Boolean(record.attachments?.length) && <ul className="mx-3.5 mt-2 space-y-1 text-[10px] text-muted-foreground">{record.attachments!.map((file) => <li key={file.uri} className="break-all select-text">补充文件：{file.name} · {file.uri}</li>)}</ul>}
          <div className="grid gap-3 p-3.5 lg:grid-cols-2">
            {record.questions.map((question, index) => {
              const answer = record.answers?.[question.qid]?.trim();
              return (
                <div key={`${question.qid}-${index}`} className="rounded-lg border border-amber-100 bg-amber-50/20 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className="grid size-5 place-items-center rounded-full bg-amber-500 text-[10px] font-bold text-white">{index + 1}</span>
                    {question.severity && <Pill tone={question.severity === "高" ? "red" : "amber"}>{question.severity}优先级</Pill>}
                    {question.field_label && <span className="text-[10px] font-semibold text-amber-800">{question.field_label}</span>}
                  </div>
                  <div className="text-[12.5px] font-semibold leading-relaxed">{question.question}</div>
                  {question.context && <div className="mt-2 rounded-md border-l-2 border-accent/40 bg-accent-soft px-2 py-1.5 text-[10px] leading-relaxed text-muted-foreground">{question.context}</div>}
                  {answer ? (
                    <div className="mt-2 rounded-md border border-success/25 bg-success/5 px-2.5 py-2 text-[11px] leading-relaxed text-foreground">
                      <span className="mr-1 font-semibold text-success">已补充：</span>
                      <span className="select-text whitespace-pre-wrap">{answer}</span>
                    </div>
                  ) : (
                    <div className="mt-2 text-[10px] text-muted-soft">未填写答案；该缺口仍需人工核对。</div>
                  )}
                  {record.unresolved?.includes(question.qid) && <div className="mt-2 text-[10px] font-semibold text-amber-800">未解决：保留证据缺口</div>}
                  {(record.references?.[question.qid] || []).map((uri) => <div key={uri} className="mono mt-1 break-all text-[10px] text-muted-foreground select-text">Evidence：{uri}</div>)}
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

function isMarkdownArtifact(artifact: Pick<ArtifactContent, "name" | "path"> | null) {
  if (!artifact) return false;
  return /\.(?:md|markdown)$/i.test(artifact.name) || /\.(?:md|markdown)$/i.test(artifact.path);
}

function formatDateTime(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
