import { useCallback, useEffect, useRef, useState } from "react";
import { PageHeader, Panel, StatCard, Pill, type PillTone } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, type UserProfile } from "@/api/client";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import DropZone from "@/views/skills/DropZone";
import MarkdownWorkspace, { MarkdownDocument } from "@/components/MarkdownWorkspace";
import MiningWhiteboxPanel from "@/views/MiningWhiteboxPanel";
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
  FolderInput,
  Trash2,
  ListChecks,
  Clock3,
  CircleStop,
  ChevronDown,
  Pencil,
  Save,
  Send,
} from "lucide-react";

/**
 * MiningView — SkillMiner「文档 → Skill」挖掘流水线控制台。
 *
 * 集成后 teamEvolver 统一控制台「挖掘」分组的落地页。挖掘能力在左侧边栏拆成
 * 3 个独立菜单项（总览 / 知识源 / 挖掘任务），本组件
 * 按 `page` 渲染对应页面。
 *
 * 后端已接入：teamEvolver 服务把内嵌的 SkillMiner 控制台（子进程）反向代理到
 * ``/api/mining/*``。本视图通过 ``/api/mining/config`` 管理知识源，并通过
 * ``/api/mining/jobs`` 创建、调度和跟踪持久化挖掘任务。
 */

export type MinePage = "overview" | "sources" | "jobs";

const PAGE_META: Record<MinePage, { title: string; description: string }> = {
  overview: { title: "挖掘总览", description: "查看知识源、挖掘任务与产物编译状态，快速掌握 SkillMiner 当前工作负载。" },
  sources: { title: "知识源", description: "管理用于技能挖掘的文档目录，支持上传、新建、重命名、合并与删除。" },
  jobs: { title: "挖掘任务", description: "并行创建和跟踪挖掘任务；任务完成后可审核、编辑产物并提交进化。" },
};

// ---- Backend types (subset of SkillMiner /api/config & SSE events) -------- //

type SourceIngestionStatusValue = "empty" | "processing" | "ready" | "failed";

interface SourceIngestionStatus {
  schema_version: number;
  source_path: string;
  batch_id: string;
  status: SourceIngestionStatusValue;
  stage: "idle" | "validating" | "converting" | "writing" | "merging" | "complete" | "failed" | string;
  progress: number;
  processed_files: number;
  total_files: number;
  current_file: string;
  error: string;
  started_at: string;
  updated_at: string;
  finished_at: string;
}

interface InputSource {
  path: string;
  document_count: number;
  total_bytes: number;
  ready: boolean;
  ingestion?: SourceIngestionStatus;
}

interface CompiledSkillDetail {
  name: string;
  has_skill: boolean;
  has_evaluation: boolean;
  has_benchmark: boolean;
  question_count: number;
}

interface MiningArtifact {
  name: string;
  kind: "skill" | "evaluation" | "benchmark" | "semantic";
  path: string;
  size_bytes: number;
  skill_name: string;
}

interface MiningRound {
  round: number;
  artifacts: MiningArtifact[];
  skills?: CompiledSkillDetail[];
}

type MiningJobStatus =
  | "preparing"
  | "queued"
  | "running"
  | "waiting"
  | "stopping"
  | "stopped"
  | "succeeded"
  | "failed"
  | "interrupted";

interface MiningJob {
  job_id: string;
  name: string;
  status: MiningJobStatus;
  input_dir: string;
  document_count: number | null;
  max_rounds: number;
  current_round: number;
  phase: PhaseState;
  created_at: string;
  started_at: string;
  finished_at: string;
  updated_at: string;
  error: string;
  stop_reason: string;
  legacy?: boolean;
  artifacts?: MiningArtifact[];
  rounds?: MiningRound[];
  logs?: string[];
  skills?: CompiledSkillDetail[];
  pending_checkpoint?: HumanCheckpoint | null;
  knowledge_gaps?: {
    total: number;
    questions: HumanCheckpointQuestion[];
  } | null;
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

interface KnowledgeSourceFile {
  relative_path: string;
  name: string;
  size_bytes: number;
  updated_at: string;
}

interface KnowledgeSourceFileContent {
  source_path: string;
  relative_path: string;
  name: string;
  size_bytes: number;
  preview_available: boolean;
  truncated: boolean;
  content: string;
  message: string;
}

interface MiningConfig {
  input_dirs: string[];
  input_sources: InputSource[];
  default_input_dir: string;
  max_rounds_default: number;
  max_rounds_range: [number, number];
  compiled_skills: string[];
  compiled_skill_details: CompiledSkillDetail[];
  benchmark: {
    default_dist: string;
    default_total: number;
  };
  checkpoints: { key: string; label: string; desc: string }[];
}

interface KnowledgeUploadResult {
  ok: boolean;
  batch_id: string;
  written: Array<{
    name: string;
    path: string;
    original_name: string;
    size_bytes: number;
    normalized_size_bytes: number;
    renamed: boolean;
    converted: boolean;
    source_format: string;
    source_encoding: string;
  }>;
  source: InputSource;
}

interface PhaseState {
  step1: "idle" | "active" | "done";
  step2: "idle" | "active" | "done";
  step3: "idle" | "active" | "done";
}

const STEP_META = [
  { key: "step1" as const, n: 1, title: "样本包构建", sub: "输入文档 → sample_packages", icon: Boxes },
  { key: "step2" as const, n: 2, title: "语义发现", sub: "决策单元 + 知识缺口", icon: ScanSearch },
  { key: "step3" as const, n: 3, title: "Skill 编译", sub: "SKILL.md + EVALUATION.md", icon: FileCode2 },
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

  const refreshJobs = useCallback(async (notifyFailure = false) => {
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
  }, []);

  const refreshConfig = useCallback(async (notifyFailure = true) => {
    try {
      const next = await api<MiningConfig>("/api/mining/config");
      setConfig(next);
      await refreshJobs(false);
      return next;
    } catch (e: any) {
      if (notifyFailure) toastErr("加载挖掘配置失败", e.message);
      return null;
    }
  }, [refreshJobs]);

  useEffect(() => {
    if (!active) return;
    refreshConfig(true);
    const timer = window.setInterval(() => refreshConfig(false), 2000);
    return () => window.clearInterval(timer);
  }, [active, refreshConfig]);

  return { config, jobs, jobSummary, refreshConfig, refreshJobs };
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
  const [humanCheckpoints, setHumanCheckpoints] = useState(true);
  const [taskName, setTaskName] = useState("");
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<SourceIngestionStatus | null>(null);
  const [selectedInputDir, setSelectedInputDir] = useState("");
  const [uploadTarget, setUploadTarget] = useState("");
  const [newUploadSourceName, setNewUploadSourceName] = useState("");
  const [createSourceOpen, setCreateSourceOpen] = useState(false);
  const [createSourceName, setCreateSourceName] = useState("");
  const [mergeSourceOpen, setMergeSourceOpen] = useState(false);
  const [mergeSourcePaths, setMergeSourcePaths] = useState<string[]>([]);
  const [mergeTargetName, setMergeTargetName] = useState("");
  const [deleteSource, setDeleteSource] = useState<InputSource | null>(null);
  const [sourceMutating, setSourceMutating] = useState(false);
  const [renamingSourcePath, setRenamingSourcePath] = useState("");
  const [renameSourceName, setRenameSourceName] = useState("");
  const [renameSourceError, setRenameSourceError] = useState("");
  const [renamePendingPath, setRenamePendingPath] = useState("");
  const [sourcePreview, setSourcePreview] = useState<InputSource | null>(null);
  const [sourceFiles, setSourceFiles] = useState<KnowledgeSourceFile[]>([]);
  const [sourceFilesTruncated, setSourceFilesTruncated] = useState(false);
  const [sourcePreviewLoading, setSourcePreviewLoading] = useState(false);
  const [selectedSourceFile, setSelectedSourceFile] = useState<KnowledgeSourceFile | null>(null);
  const [sourceFileContent, setSourceFileContent] = useState<KnowledgeSourceFileContent | null>(null);
  const [sourceFileLoading, setSourceFileLoading] = useState("");
  const cancelledRenamePath = useRef("");
  const [jobFilter, setJobFilter] = useState<"all" | "active" | "completed" | "failed">("all");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [selectedJob, setSelectedJob] = useState<MiningJob | null>(null);
  const [jobActionPending, setJobActionPending] = useState(false);
  const [deleteJob, setDeleteJob] = useState<MiningJob | null>(null);
  const [jobDeleting, setJobDeleting] = useState(false);
  const [checkpointAnswers, setCheckpointAnswers] = useState<Record<string, string>>({});
  const [checkpointSubmitting, setCheckpointSubmitting] = useState(false);
  const [knowledgeSupplementExpanded, setKnowledgeSupplementExpanded] = useState(false);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactContent | null>(null);
  const [artifactDraft, setArtifactDraft] = useState("");
  const [artifactEditing, setArtifactEditing] = useState(false);
  const [artifactEditable, setArtifactEditable] = useState(false);
  const [artifactSaving, setArtifactSaving] = useState(false);
  const [loadingArtifact, setLoadingArtifact] = useState("");
  const [submittingJobId, setSubmittingJobId] = useState("");
  const [createJobOpen, setCreateJobOpen] = useState(false);
  const artifactIsMarkdown = isMarkdownArtifact(artifactPreview);
  const defaultRoundsInitialized = useRef(false);

  // Config refreshes every two seconds.  Apply its default only once, so a
  // user's in-progress slider choice is never overwritten by that polling.
  useEffect(() => {
    if (!config) return;
    if (!defaultRoundsInitialized.current && config.max_rounds_default) {
      setRounds(config.max_rounds_default);
      defaultRoundsInitialized.current = true;
    }
    const next = preferredInputDir && config.input_dirs.includes(preferredInputDir)
      ? preferredInputDir
      : selectedInputDir && config.input_dirs.includes(selectedInputDir)
        ? selectedInputDir
        : config.default_input_dir || config.input_dirs[0] || "data/input";
    setSelectedInputDir(next);
    if (!uploadTarget || (!config.input_dirs.includes(uploadTarget) && uploadTarget !== "__new__")) {
      setUploadTarget(next);
    }
    onInputDirChange?.(next);
  }, [config, preferredInputDir]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active || page !== "jobs" || !selectedJobId) return;
    let cancelled = false;
    async function loadDetail() {
      try {
        const response = await api<{ ok: boolean; job: MiningJob }>(
          `/api/mining/jobs/${encodeURIComponent(selectedJobId)}`
        );
        if (!cancelled) setSelectedJob(response.job);
      } catch (e: any) {
        if (!cancelled) toastErr("加载任务详情失败", e.message);
      }
    }
    loadDetail();
    const selected = mining.jobs.find((job) => job.job_id === selectedJobId);
    const timer = selected && isActiveJob(selected.status)
      ? window.setInterval(loadDetail, 2000)
      : undefined;
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
  }, [active, page, selectedJobId, mining.jobs]);

  useEffect(() => {
    setCheckpointAnswers({});
  }, [selectedJob?.pending_checkpoint?.id]);

  useEffect(() => {
    setKnowledgeSupplementExpanded(false);
  }, [selectedJobId, selectedJob?.pending_checkpoint?.id]);

  if (!active) return null;

  const meta = PAGE_META[page];
  const inputDir = selectedInputDir || config?.default_input_dir || "data/input";
  const maxRounds = config?.max_rounds_range?.[1] ?? 5;
  const minRounds = config?.max_rounds_range?.[0] ?? 1;
  const inputSources = config?.input_sources ?? [];
  const selectedSource = inputSources.find((source) => source.path === inputDir);
  const uploadTargetSource = inputSources.find((source) => source.path === uploadTarget);
  const selectedIngestion = selectedSource ? sourceIngestion(selectedSource) : null;
  const uploadTargetIngestion = uploadTargetSource ? sourceIngestion(uploadTargetSource) : null;
  const jobCounts = {
    total: mining.jobs.length,
    running: mining.jobs.filter((job) => isActiveJob(job.status) && job.status !== "queued" && job.status !== "preparing").length,
    queued: mining.jobs.filter((job) => job.status === "queued" || job.status === "preparing").length,
    completed: mining.jobs.filter((job) => job.status === "succeeded").length,
    failed: mining.jobs.filter((job) => job.status === "failed" || job.status === "interrupted" || job.status === "stopped").length,
  };
  const sourceDocumentTotal = inputSources.reduce((sum, source) => sum + source.document_count, 0);
  const pipelineJobs = mining.jobs.filter(
    (job) => job.status === "running" || job.status === "waiting" || job.status === "stopping"
  );
  const recentTaskCount = Math.min(5, jobCounts.total);
  const running = jobCounts.running > 0 || jobCounts.queued > 0;
  const stateLabel = [
    jobCounts.running ? `${jobCounts.running} 个运行中` : "",
    jobCounts.queued ? `${jobCounts.queued} 个排队中` : "",
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
    onInputDirChange?.(path);
  }

  function validateSourceRename(source: InputSource, value: string) {
    const name = value.trim();
    if (!name) return "知识源名称不能为空";
    if (name.startsWith(".") || name.length > 80 || name.includes("/") || name.includes("\\")) {
      return "名称不能以 . 开头、超过 80 个字符或包含 /、\\";
    }
    const normalized = name.toLocaleLowerCase();
    const duplicate = inputSources.find((item) => (
      item.path !== source.path && sourceDisplayName(item.path).toLocaleLowerCase() === normalized
    ));
    return duplicate ? `已存在同名知识源：${sourceDisplayName(duplicate.path)}` : "";
  }

  function beginSourceRename(source: InputSource) {
    if (renamePendingPath) return;
    cancelledRenamePath.current = "";
    setRenamingSourcePath(source.path);
    setRenameSourceName(sourceDisplayName(source.path));
    setRenameSourceError("");
  }

  function cancelSourceRename(source: InputSource, input: HTMLInputElement) {
    cancelledRenamePath.current = source.path;
    setRenamingSourcePath("");
    setRenameSourceName("");
    setRenameSourceError("");
    input.blur();
  }

  async function commitSourceRename(source: InputSource) {
    if (cancelledRenamePath.current === source.path) {
      cancelledRenamePath.current = "";
      return;
    }
    if (renamingSourcePath !== source.path || renamePendingPath) return;

    const name = renameSourceName.trim();
    const validationError = validateSourceRename(source, name);
    if (validationError) {
      setRenameSourceError(validationError);
      return;
    }
    if (name === sourceDisplayName(source.path)) {
      setRenamingSourcePath("");
      setRenameSourceName("");
      setRenameSourceError("");
      return;
    }

    setRenamePendingPath(source.path);
    setRenameSourceError("");
    try {
      const response = await api<{ source: InputSource; previous_path: string }>(
        `/api/mining/sources/${encodeURIComponent(sourceDisplayName(source.path))}/rename`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        }
      );
      if (selectedInputDir === source.path) {
        setSelectedInputDir(response.source.path);
        onInputDirChange?.(response.source.path);
      }
      if (uploadTarget === source.path) setUploadTarget(response.source.path);
      await mining.refreshConfig();
      setRenamingSourcePath("");
      setRenameSourceName("");
      toastOk(
        "知识源已重命名",
        `${sourceDisplayName(source.path)} → ${sourceDisplayName(response.source.path)}`
      );
    } catch (e: any) {
      setRenameSourceError(e.message);
      toastErr("重命名知识源失败", e.message);
    } finally {
      setRenamePendingPath("");
    }
  }

  async function uploadKnowledgeFiles(list: FileList) {
    // FileList belongs to the hidden <input>. DropZone clears that input as
    // soon as this callback returns, so keep a stable snapshot before any
    // await (notably before a new source would otherwise be created).
    const selectedFiles = Array.from(list);
    if (!selectedFiles.length) return;
    setUploading(true);
    let progressTimer: number | undefined;
    try {
      let target = uploadTarget || inputDir;
      let createSource = false;
      if (target === "__new__") {
        const name = newUploadSourceName.trim();
        if (!name) throw new Error("请输入新知识源名称");
        const duplicate = inputSources.some(
          (source) => sourceDisplayName(source.path).toLocaleLowerCase() === name.toLocaleLowerCase()
        );
        if (duplicate) throw new Error(`知识源已存在：${name}`);
        target = `data/${name}`;
        createSource = true;
      }
      const batchId = globalThis.crypto?.randomUUID?.() || `upload-${Date.now()}`;
      setUploadProgress({
        schema_version: 1,
        source_path: target,
        batch_id: batchId,
        status: "processing",
        stage: "validating",
        progress: 0,
        processed_files: 0,
        total_files: selectedFiles.length,
        current_file: "",
        error: "",
        started_at: "",
        updated_at: "",
        finished_at: "",
      });
      const files = await Promise.all(selectedFiles.map(async (file) => ({
        name: file.name,
        content_b64: await fileToB64(file),
      })));
      let pollInFlight = false;
      const pollProgress = async () => {
        if (pollInFlight) return;
        pollInFlight = true;
        try {
          const response = await api<{ source: InputSource }>(
            `/api/mining/sources/status?source_path=${encodeURIComponent(target)}`
          );
          const ingestion = response.source.ingestion;
          if (ingestion?.batch_id === batchId) {
            setUploadProgress(ingestion);
          }
        } catch {
          // The upload request remains authoritative; a transient polling
          // failure should not turn a successful conversion into an error.
        } finally {
          pollInFlight = false;
        }
      };
      progressTimer = window.setInterval(pollProgress, 400);
      const result = await api<KnowledgeUploadResult>("/api/mining/sources/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_path: target,
          batch_id: batchId,
          create_source: createSource,
          files,
        }),
      });
      if (result.source.ingestion) setUploadProgress(result.source.ingestion);
      const renamed = result.written.filter((file) => file.renamed).length;
      const converted = result.written.filter((file) => file.converted).length;
      const details = [
        converted ? `${converted} 个文件已转为 Markdown` : "",
        renamed ? `${renamed} 个重名文件已保留为新副本` : "",
      ].filter(Boolean).join("；");
      toastOk(
        `已上传 ${result.written.length} 个文档`,
        details || `已写入 ${sourceDisplayName(result.source.path)}`
      );
      await mining.refreshConfig();
      setUploadTarget(result.source.path);
      setNewUploadSourceName("");
    } catch (e: any) {
      await mining.refreshConfig(false);
      toastErr("上传知识文档失败", e.message);
    } finally {
      if (progressTimer) window.clearInterval(progressTimer);
      setUploading(false);
      setUploadProgress(null);
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
          input_dir: inputDir,
          max_rounds: rounds,
          human_checkpoints: humanCheckpoints,
        }),
      });
      const created = response.jobs?.[0];
      if (created) setSelectedJobId(created.job_id);
      await mining.refreshJobs(false);
      setTaskName("");
      setCreateJobOpen(false);
      toastOk("挖掘任务已创建", `输入 ${sourceDisplayName(inputDir)} · 最多 ${rounds} 轮`);
      onNavigate?.("jobs");
    } catch (e: any) {
      toastErr("启动挖掘失败", e.message);
    } finally {
      setStarting(false);
    }
  }

  async function createSource() {
    const name = createSourceName.trim();
    if (!name) return;
    setSourceMutating(true);
    try {
      const response = await api<{ source: InputSource }>("/api/mining/sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await mining.refreshConfig();
      setUploadTarget(response.source.path);
      setCreateSourceOpen(false);
      setCreateSourceName("");
      toastOk("知识源已创建", sourceDisplayName(response.source.path));
    } catch (e: any) {
      toastErr("创建知识源失败", e.message);
    } finally {
      setSourceMutating(false);
    }
  }

  async function deleteKnowledgeSource() {
    if (!deleteSource) return;
    setSourceMutating(true);
    try {
      const name = deleteSource.path.split("/").pop() || "";
      await api(`/api/mining/sources/${encodeURIComponent(name)}`, { method: "DELETE" });
      setDeleteSource(null);
      await mining.refreshConfig();
      toastOk("知识源已删除", sourceDisplayName(deleteSource.path));
    } catch (e: any) {
      toastErr("删除知识源失败", e.message);
    } finally {
      setSourceMutating(false);
    }
  }

  async function mergeSources() {
    if (mergeSourcePaths.length < 2 || !mergeTargetName.trim()) return;
    setSourceMutating(true);
    try {
      const response = await api<{ source: InputSource; copied: unknown[] }>("/api/mining/sources/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_paths: mergeSourcePaths, target_name: mergeTargetName.trim() }),
      });
      await mining.refreshConfig();
      setUploadTarget(response.source.path);
      setMergeSourceOpen(false);
      setMergeSourcePaths([]);
      setMergeTargetName("");
      toastOk("知识源已合并", `${sourceDisplayName(response.source.path)} · ${response.copied.length} 个文件`);
    } catch (e: any) {
      toastErr("合并知识源失败", e.message);
    } finally {
      setSourceMutating(false);
    }
  }

  async function loadKnowledgeSourceFile(source: InputSource, file: KnowledgeSourceFile) {
    setSelectedSourceFile(file);
    setSourceFileLoading(file.relative_path);
    setSourceFileContent(null);
    try {
      const result = await api<KnowledgeSourceFileContent>(
        `/api/mining/sources/content?source_path=${encodeURIComponent(source.path)}&relative_path=${encodeURIComponent(file.relative_path)}`
      );
      setSourceFileContent(result);
    } catch (e: any) {
      toastErr("读取知识文件失败", e.message);
    } finally {
      setSourceFileLoading("");
    }
  }

  async function openKnowledgeSourcePreview(source: InputSource) {
    setSourcePreview(source);
    setSourceFiles([]);
    setSourceFilesTruncated(false);
    setSelectedSourceFile(null);
    setSourceFileContent(null);
    setSourcePreviewLoading(true);
    try {
      const result = await api<{ files: KnowledgeSourceFile[]; truncated: boolean }>(
        `/api/mining/sources/files?source_path=${encodeURIComponent(source.path)}`
      );
      const files = result.files || [];
      setSourceFiles(files);
      setSourceFilesTruncated(Boolean(result.truncated));
      if (files[0]) await loadKnowledgeSourceFile(source, files[0]);
    } catch (e: any) {
      toastErr("加载知识源内容失败", e.message);
    } finally {
      setSourcePreviewLoading(false);
    }
  }

  async function stopJob(job: MiningJob) {
    if (job.legacy) return;
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

  async function submitCheckpointAnswers(job: MiningJob) {
    const checkpoint = job.pending_checkpoint;
    if (!checkpoint) return;
    setCheckpointSubmitting(true);
    try {
      const response = await api<{ job: MiningJob }>(
        `/api/mining/jobs/${encodeURIComponent(job.job_id)}/answer`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question_id: checkpoint.id,
            answers: checkpointAnswers,
          }),
        }
      );
      setSelectedJob(response.job);
      setCheckpointAnswers({});
      await mining.refreshJobs(false);
      toastOk("补证信息已提交", "挖掘任务将带着你的答案继续运行");
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
      setArtifactDraft(result.content);
      setArtifactEditing(false);
      setArtifactEditable(editable);
    } catch (e: any) {
      toastErr("读取历史产物失败", e.message);
    } finally {
      setLoadingArtifact("");
    }
  }

  async function saveArtifactRevision() {
    if (!artifactPreview) return;
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
      toastOk("产物修改已保存", "提交到进化时将使用当前版本");
    } catch (e: any) {
      toastErr("保存产物失败", e.message);
    } finally {
      setArtifactSaving(false);
    }
  }

  async function submitJobToEvolution(job: MiningJob) {
    if (job.status !== "succeeded") return;
    const skillNames = jobSkillNames(job);
    if (!skillNames.length) {
      toastErr("无法提交到进化", "任务中没有完整的 Skill 产物");
      return;
    }
    setSubmittingJobId(job.job_id);
    try {
      const results = [];
      for (const skillName of skillNames) {
        const skillArtifact = jobArtifacts(job).find(
          (artifact) => artifact.kind === "skill" && artifact.skill_name === skillName
        );
        results.push(await api<{ created: boolean; job_id: string; skill_name: string }>(
          `/api/mined-jobs/${encodeURIComponent(job.job_id)}/skills/${encodeURIComponent(skillName)}/submit`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ artifact_path: skillArtifact?.path || "" }),
          }
        ));
      }
      const created = results.filter((result) => result.created).length;
      toastOk(
        "已提交到进化候选区",
        created
          ? `${created} 个 Skill 已生成候选，等待评测与人工审核`
          : "当前产物版本已在候选区中"
      );
      onNavigate?.("candidates");
    } catch (e: any) {
      toastErr("提交到进化失败", e.message);
    } finally {
      setSubmittingJobId("");
    }
  }

  function renderJobDetail(job: MiningJob) {
    const durableGaps = job.knowledge_gaps?.questions || [];
    const supplementQuestions = job.pending_checkpoint?.questions || durableGaps;
    const hasKnowledgeSupplement = supplementQuestions.length > 0;
    return (
      <div className="space-y-4 border-t border-accent/20 bg-accent-soft/35 px-5 py-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="select-text truncate text-[15px] font-bold">{job.name}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <Pill tone={jobStatusMeta(job.status).tone as any}>{jobStatusMeta(job.status).label}</Pill>
              {job.input_dir && <span className="mono select-text">{sourceDisplayName(job.input_dir)}</span>}
              {job.document_count !== null && <span>{job.document_count} 个文档</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {isActiveJob(job.status) && !job.legacy && (
              <Button size="sm" variant="outline" disabled={jobActionPending || job.status === "stopping"} onClick={() => stopJob(job)}>
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

        {hasKnowledgeSupplement && (
          <div className="overflow-hidden rounded-xl border border-amber-300/70 bg-amber-50/80 shadow-sm">
            <div className={cn("flex flex-wrap items-center justify-between gap-3 px-4 py-3.5", knowledgeSupplementExpanded && "border-b border-amber-200")}>
              <div className="flex min-w-0 items-center gap-2.5">
                <ListChecks className="size-4 shrink-0 text-amber-700" />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[14px] font-bold">知识补充</span>
                    {job.pending_checkpoint
                      ? <Pill tone="amber">等待填写</Pill>
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
                                value={checkpointAnswers[question.qid] || ""}
                                placeholder={question.placeholder || "请输入具体答案"}
                                onChange={(event) => setCheckpointAnswers((current) => ({ ...current, [question.qid]: event.target.value }))}
                              />
                            ) : (
                              <textarea
                                rows={3}
                                value={checkpointAnswers[question.qid] || ""}
                                placeholder={question.placeholder || "请输入具体答案"}
                                onChange={(event) => setCheckpointAnswers((current) => ({ ...current, [question.qid]: event.target.value }))}
                                className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-[12px] leading-relaxed outline-none transition-colors focus:border-accent"
                              />
                            )}
                          </label>
                        </div>
                      ))}
                    </div>

                    <div className="mt-4 flex items-center justify-between gap-3 border-t border-amber-200 pt-3">
                      <span className="text-[10.5px] text-muted-foreground">不确定的问题可以留空，系统会继续保留为知识缺口。</span>
                      <Button disabled={checkpointSubmitting} onClick={() => submitCheckpointAnswers(job)}>
                        {checkpointSubmitting ? "提交中…" : "提交答案并继续挖掘"}
                        <ArrowRight className="size-3.5" />
                      </Button>
                    </div>
                  </>
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
                )}
              </div>
            )}
          </div>
        )}

        {isActiveJob(job.status) ? (
          <div className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-[minmax(360px,0.8fr)_minmax(0,1.2fr)]">
            <div className="rounded-xl border border-border bg-surface p-4">
              <div className="mb-4 flex items-center justify-between text-xs">
                <span className="font-semibold">挖掘流水线</span>
                <span className="text-muted-foreground">
                  {job.status === "queued" || job.status === "preparing"
                    ? "等待调度"
                    : `第 ${job.current_round || 1} / ${job.max_rounds} 轮`}
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
                  ? job.logs.map((line, index) => <div key={index}>{formatRuntimeLog(line)}</div>)
                  : <span className="text-muted-soft">任务正在准备，暂无日志。</span>}
              </div>
            </div>
            </div>
          </div>
        ) : (
          <div className="rounded-xl border border-border bg-surface p-4">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div className="text-[13px] font-bold">任务产物</div>
              <div className="flex items-center gap-2">
                {job.finished_at && <span className="text-[10px] text-muted-soft">完成于 {formatDateTime(job.finished_at)}</span>}
                {job.status === "succeeded" && (
                  <Button
                    size="sm"
                    disabled={submittingJobId === job.job_id || !jobSkillNames(job).length}
                    onClick={() => submitJobToEvolution(job)}
                  >
                    <Send className="size-3.5" />
                    {submittingJobId === job.job_id ? "提交中…" : "提交到进化"}
                  </Button>
                )}
              </div>
            </div>
            {jobArtifacts(job).length ? (
              <div className="grid gap-2 md:grid-cols-2">
                {jobArtifacts(job).map((artifact) => (
                  <button
                    key={artifact.path}
                    type="button"
                    disabled={loadingArtifact === artifact.path}
                    onClick={() => previewArtifact(artifact, job.status === "succeeded")}
                    className="flex w-full items-center gap-2 rounded-lg border border-border bg-background px-3 py-2.5 text-left transition-colors hover:border-accent"
                  >
                    <FileText className="size-4 shrink-0 text-accent" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12px] font-semibold select-text">
                        {artifact.skill_name ? `${artifact.skill_name} / ${artifact.name}` : artifact.name}
                      </span>
                      <span className="mono block truncate text-[9.5px] text-muted-soft select-text">{artifact.path}</span>
                    </span>
                    <Pill tone={artifactTone(artifact.kind) as any}>{artifactLabel(artifact.kind)}</Pill>
                    {job.status === "succeeded"
                      ? <Pencil className="size-3.5 text-muted-foreground" />
                      : <Eye className="size-3.5 text-muted-foreground" />}
                  </button>
                ))}
              </div>
            ) : (
              <div className="select-text whitespace-pre-wrap break-words rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted-soft">
                {job.error || job.stop_reason || "该任务没有产生可预览产物。"}
              </div>
            )}
          </div>
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
            <StatCard label="文档总数" value={config ? String(sourceDocumentTotal) : "—"} />
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
                          <span>{sourceDisplayName(job.input_dir)}</span>
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
                      {sourceDisplayName(job.input_dir)}
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
        <div className="space-y-[18px] px-[22px] py-[22px]">
          <Panel
            title="上传文档"
            extra={
              <div className="flex items-center gap-2">
                <Button size="sm" variant="outline" onClick={() => setCreateSourceOpen(true)}>
                  <FolderPlus className="size-3.5" /> 新建知识源
                </Button>
                <Button size="sm" variant="outline" disabled={inputSources.length < 2} onClick={() => setMergeSourceOpen(true)}>
                  <FolderInput className="size-3.5" /> 合并知识源
                </Button>
              </div>
            }
          >
            <div className="grid gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_260px]">
              <div>
                <DropZone
                  multiple
                  accept=".md,.markdown,.txt,.docx,.xlsx,.pdf"
                  disabled={
                    uploading
                    || !config
                    || uploadTargetIngestion?.status === "processing"
                    || (uploadTarget === "__new__" && !newUploadSourceName.trim())
                  }
                  onFiles={uploadKnowledgeFiles}
                  label={
                    uploading
                      ? "正在上传并校验文档…"
                      : "点击选择或将多个知识文档拖拽到这里"
                  }
                />
                <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                  支持 Markdown、TXT、Word（.docx）、Excel（.xlsx）和 PDF，上传后统一转为 Markdown；扫描版 PDF 需要先完成 OCR。
                  单文件不超过 10 MB，单次最多 50 个、合计不超过 40 MB。
                </p>
                {uploading && uploadProgress && (
                  <div className="mt-3 rounded-lg border border-border bg-background/70 p-3">
                    <div className="mb-2 flex items-center justify-between gap-3 text-[11px] font-semibold">
                      <span className="min-w-0 truncate">
                        {ingestionStageLabel(uploadProgress.stage)}
                        {uploadProgress.current_file ? ` · ${uploadProgress.current_file}` : ""}
                      </span>
                      <span className="mono shrink-0 text-accent">{uploadProgress.progress}%</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-accent transition-[width] duration-300"
                        style={{ width: `${uploadProgress.progress}%` }}
                      />
                    </div>
                    <div className="mt-1.5 text-[10.5px] text-muted-foreground">
                      {uploadProgress.processed_files} / {uploadProgress.total_files} 个文件
                    </div>
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-border bg-background/60 p-3.5">
                <Label htmlFor="knowledge-source-target" className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  上传到知识源
                </Label>
                <select
                  id="knowledge-source-target"
                  value={uploadTarget || inputDir}
                  disabled={uploading || !config}
                  onChange={(e) => setUploadTarget(e.target.value)}
                  className="mono w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                >
                  {(config?.input_dirs ?? [inputDir]).map((dir) => (
                    <option key={dir} value={dir}>{sourceDisplayName(dir)}</option>
                  ))}
                  <option value="__new__">＋ 上传为新知识源</option>
                </select>
                {uploadTarget === "__new__" && (
                  <Input
                    className="mt-2"
                    value={newUploadSourceName}
                    onChange={(event) => setNewUploadSourceName(event.target.value)}
                    placeholder="新知识源名称，如 abc"
                    maxLength={80}
                  />
                )}
                <div className="mt-3 flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground">
                  <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success" />
                  <span>不会覆盖同名文件；系统会自动添加序号，并统一保存为 UTF-8 文本。</span>
                </div>
              </div>
            </div>
          </Panel>

          <Panel
            title="知识源目录"
            count={config ? `${inputSources.length} 个目录` : undefined}
          >
            {!config ? (
              <div className="p-6 text-center text-sm text-muted-soft">加载中…</div>
            ) : inputSources.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted-soft">
                尚无知识源。请先新建知识源并上传待挖掘文档。
              </div>
            ) : (
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-line text-[11px] uppercase tracking-wide text-muted-foreground">
                    <th className="px-4 py-2.5 text-left font-semibold">目录名称</th>
                    <th className="px-4 py-2.5 text-left font-semibold">文档</th>
                    <th className="px-4 py-2.5 text-left font-semibold">状态</th>
                    <th className="px-4 py-2.5 text-right font-semibold">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {inputSources.map((source) => (
                    <tr key={source.path} className="border-b border-line last:border-none">
                      <td className="px-4 py-3">
                        <div className="flex min-w-[220px] items-center font-semibold">
                          <div className="group/rename relative min-w-0 max-w-[320px] flex-1">
                            <input
                              value={renamingSourcePath === source.path ? renameSourceName : sourceDisplayName(source.path)}
                              onFocus={() => beginSourceRename(source)}
                              onChange={(event) => {
                                const value = event.target.value;
                                setRenameSourceName(value);
                                setRenameSourceError(validateSourceRename(source, value));
                              }}
                              onBlur={() => commitSourceRename(source)}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") event.currentTarget.blur();
                                if (event.key === "Escape") {
                                  event.preventDefault();
                                  cancelSourceRename(source, event.currentTarget);
                                }
                              }}
                              disabled={Boolean(renamePendingPath) || sourceIngestion(source).status === "processing"}
                              aria-label={`重命名知识源 ${sourceDisplayName(source.path)}`}
                              aria-invalid={renamingSourcePath === source.path && Boolean(renameSourceError)}
                              aria-describedby={renamingSourcePath === source.path && renameSourceError ? `rename-error-${encodeURIComponent(source.path)}` : undefined}
                              className={cn(
                                "mono h-7 w-full rounded-md border border-transparent bg-transparent py-1 pl-1 pr-7 text-[13px] font-semibold outline-none transition-colors",
                                "hover:border-input hover:bg-background focus:border-ring focus:bg-background focus:ring-2 focus:ring-ring/20",
                                renamingSourcePath === source.path && renameSourceError && "border-destructive bg-background ring-2 ring-destructive/15"
                              )}
                            />
                            <Pencil className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-muted-soft transition-colors group-hover/rename:text-muted-foreground" />
                          </div>
                        </div>
                        {renamingSourcePath === source.path && renameSourceError && (
                          <div id={`rename-error-${encodeURIComponent(source.path)}`} role="alert" className="mt-1 text-[11px] text-destructive">
                            {renameSourceError}
                          </div>
                        )}
                        {(renamePendingPath === source.path || source.path === config.default_input_dir) && (
                          <div className="mt-0.5 text-[11px] text-muted-foreground">
                            {renamePendingPath === source.path ? "正在保存新名称…" : "默认挖掘输入"}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {source.document_count} 个 · {formatBytes(source.total_bytes)}
                      </td>
                      <td className="px-4 py-3">
                        <Pill tone={sourceStatusMeta(source).tone}>{sourceStatusMeta(source).label}</Pill>
                        {sourceIngestion(source).status === "processing" && (
                          <div className="mt-2 w-28">
                            <div className="h-1 overflow-hidden rounded-full bg-muted">
                              <div
                                className="h-full rounded-full bg-accent transition-[width] duration-300"
                                style={{ width: `${sourceIngestion(source).progress}%` }}
                              />
                            </div>
                            <div className="mono mt-1 text-[10px] text-muted-foreground">
                              {sourceIngestion(source).progress}%
                            </div>
                          </div>
                        )}
                        {sourceIngestion(source).status === "failed" && sourceIngestion(source).error && (
                          <div className="mt-1 max-w-52 truncate text-[10px] text-destructive" title={sourceIngestion(source).error}>
                            {sourceIngestion(source).error}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex justify-end gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={source.document_count === 0}
                            onClick={() => openKnowledgeSourcePreview(source)}
                          >
                            <Eye className="size-3.5" /> 查看内容
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!source.ready}
                            onClick={() => {
                              selectInputDir(source.path);
                              onNavigate?.("jobs");
                            }}
                          >
                            用于挖掘 <ArrowRight className="size-3.5" />
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={sourceIngestion(source).status === "processing"}
                            className="text-destructive hover:text-destructive"
                            onClick={() => setDeleteSource(source)}
                            aria-label={`删除 ${sourceDisplayName(source.path)}`}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      )}

      {/* ---- 挖掘任务 ---- */}
      {page === "jobs" && (
        <div className="space-y-[18px] px-[22px] py-[22px]">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StatCard label="全部任务" value={String(jobCounts.total)} />
            <StatCard label="运行中" value={String(jobCounts.running)} />
            <StatCard label="排队中" value={String(jobCounts.queued)} />
            <StatCard label="已完成" value={String(jobCounts.completed)} />
            <StatCard label="异常 / 停止" value={String(jobCounts.failed)} />
          </div>

          <Panel
            title="挖掘任务"
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
                  <FolderPlus className="size-3.5" /> 新建挖掘任务
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
                        <span><Pill tone={status.tone as any}>{status.label}</Pill></span>
                        <span className="min-w-0 pr-4">
                          <span className="block truncate font-semibold select-text">{job.name}</span>
                          <span className="mono mt-0.5 block truncate text-[10px] text-muted-soft select-text">{job.job_id}</span>
                        </span>
                        <span className="mono truncate pr-3 text-[11px] text-muted-foreground select-text">
                          {job.input_dir ? sourceDisplayName(job.input_dir) : "历史归档"}
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
            <DialogTitle>新建挖掘任务</DialogTitle>
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
                  <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">输入文档目录</span>
                  {config && config.input_dirs.length > 1 ? (
                    <select
                      value={inputDir}
                      disabled={starting}
                      onChange={(event) => selectInputDir(event.target.value)}
                      className="mono w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                    >
                      {inputSources.map((source) => (
                        <option key={source.path} value={source.path} disabled={!source.ready}>
                          {sourceDisplayName(source.path)}{source.ready ? "" : `（${sourceStatusMeta(source).label}）`}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div className="mono rounded-lg border border-border bg-background px-3 py-2 text-[13px]">
                      {sourceDisplayName(inputDir)}
                    </div>
                  )}
                  <div className={cn(
                    "mt-1.5 text-[11px]",
                    selectedSource?.ready ? "text-muted-foreground" : selectedIngestion?.status === "failed" ? "text-destructive" : "text-amber-700"
                  )}>
                    {!config
                      ? "正在检查目录…"
                      : selectedSource?.ready
                        ? `已检测到 ${selectedSource.document_count} 个文档 · ${formatBytes(selectedSource.total_bytes)}`
                        : selectedIngestion?.status === "processing"
                          ? `知识源正在后处理（${selectedIngestion.progress}%），完成前不可用于挖掘。`
                          : selectedIngestion?.status === "failed"
                            ? `知识源后处理失败：${selectedIngestion.error || "请重新上传文件"}`
                            : "该目录没有非隐藏文档，请先上传素材。"}
                  </div>
                </label>

                <label className="block">
                  <span className="mb-1.5 flex items-center justify-between text-xs font-semibold text-muted-foreground">
                    反思环最大轮数
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

                <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border bg-background p-3.5 transition-colors hover:border-accent/50">
                  <input
                    type="checkbox"
                    checked={humanCheckpoints}
                    disabled={starting}
                    onChange={(event) => setHumanCheckpoints(event.target.checked)}
                    className="mt-0.5 size-4 accent-[var(--accent)]"
                  />
                  <span>
                    <span className="block text-[12px] font-semibold">启用知识补充</span>
                    <span className="mt-1 block text-[11px] leading-relaxed text-muted-foreground">
                      发现影响 Skill 编译的关键知识缺口时暂停并以表单提问；关闭后任务会直接完成当前挖掘流程。
                    </span>
                  </span>
                </label>

                <div className="rounded-lg border border-border bg-background/70 p-3 text-[11px] leading-relaxed text-muted-foreground">
                  新任务会自动复用“全局模型”配置，并固化知识源快照。最多同时运行 {mining.jobSummary.max_parallel} 个任务，超出后自动排队。
                </div>

                <div className="flex justify-end gap-2 pt-1">
                  <Button variant="outline" disabled={starting} onClick={() => setCreateJobOpen(false)}>取消</Button>
                  <Button onClick={startRun} disabled={starting || !selectedSource?.ready}>
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
                  反思环 · 携带缺口回跳补证（置信未收敛且有补充素材时）
                </div>

                <div className="mt-4 flex items-center gap-3 rounded-lg border border-accent/30 bg-accent-soft px-4 py-3">
                  <Upload className="size-4 shrink-0 text-accent" />
                  <div className="text-[12.5px]">
                    <span className="font-bold text-accent">编译产物交付</span>
                    <span className="text-muted-foreground"> — 完成后生成 Skill、语义报告与内部 Benchmark</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={createSourceOpen} onOpenChange={setCreateSourceOpen}>
        <DialogContent className="w-full !max-w-[460px]">
          <DialogHeader>
            <DialogTitle>新建知识源</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="create-source-name" className="mb-1.5 block text-xs font-semibold">知识源名称</Label>
              <Input
                id="create-source-name"
                autoFocus
                value={createSourceName}
                maxLength={80}
                placeholder="例如：abc"
                onChange={(event) => setCreateSourceName(event.target.value)}
                onKeyDown={(event) => event.key === "Enter" && createSource()}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setCreateSourceOpen(false)}>取消</Button>
              <Button disabled={!createSourceName.trim() || sourceMutating} onClick={createSource}>
                {sourceMutating ? "创建中…" : "创建"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={mergeSourceOpen} onOpenChange={setMergeSourceOpen}>
        <DialogContent className="w-full !max-w-[540px]">
          <DialogHeader>
            <DialogTitle>合并知识源</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label className="mb-2 block text-xs font-semibold">选择来源（至少 2 个）</Label>
              <div className="max-h-56 space-y-2 overflow-auto rounded-lg border border-border p-2">
                {inputSources.map((source) => (
                  <label key={source.path} className={cn(
                    "flex items-center gap-3 rounded-lg px-2.5 py-2",
                    source.ready ? "cursor-pointer hover:bg-surface-subtle" : "cursor-not-allowed opacity-55"
                  )}>
                    <input
                      type="checkbox"
                      disabled={!source.ready}
                      checked={mergeSourcePaths.includes(source.path)}
                      onChange={(event) => setMergeSourcePaths((current) => event.target.checked
                        ? [...current, source.path]
                        : current.filter((path) => path !== source.path))}
                      className="size-4 accent-[var(--accent)]"
                    />
                    <span className="mono min-w-0 flex-1 truncate text-[12px] font-semibold">
                      {sourceDisplayName(source.path)}
                    </span>
                    <span className="text-[11px] text-muted-foreground">{source.document_count} 个文档</span>
                  </label>
                ))}
              </div>
            </div>
            <div>
              <Label htmlFor="merge-target-name" className="mb-1.5 block text-xs font-semibold">合并后的知识源</Label>
              <Input
                id="merge-target-name"
                value={mergeTargetName}
                maxLength={80}
                placeholder="例如：all-customer-service"
                onChange={(event) => setMergeTargetName(event.target.value)}
              />
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                合并会复制文件到目标目录，不删除原知识源；同名文件自动添加序号保留。
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setMergeSourceOpen(false)}>取消</Button>
              <Button disabled={mergeSourcePaths.length < 2 || !mergeTargetName.trim() || sourceMutating} onClick={mergeSources}>
                {sourceMutating ? "合并中…" : `合并 ${mergeSourcePaths.length} 个知识源`}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteSource} onOpenChange={(open) => !open && setDeleteSource(null)}>
        <DialogContent className="w-full !max-w-[460px]">
          <DialogHeader>
            <DialogTitle>删除知识源</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm leading-relaxed text-muted-foreground">
              确定删除 <span className="mono font-semibold text-foreground">
                {deleteSource ? sourceDisplayName(deleteSource.path) : ""}
              </span>
              ？目录内 {deleteSource?.document_count ?? 0} 个文档会一并删除。已创建任务使用独立快照，不受影响。
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setDeleteSource(null)}>取消</Button>
              <Button disabled={sourceMutating} onClick={deleteKnowledgeSource} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
                <Trash2 className="size-3.5" /> {sourceMutating ? "删除中…" : "确认删除"}
              </Button>
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
              此操作会永久删除该任务的输入快照、运行日志、知识补充记录以及全部 Skill、语义报告和 Benchmark 产物，不能恢复。
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
        open={!!sourcePreview}
        onOpenChange={(open) => {
          if (!open) {
            setSourcePreview(null);
            setSourceFiles([]);
            setSelectedSourceFile(null);
            setSourceFileContent(null);
            setSourceFileLoading("");
          }
        }}
      >
        <DialogContent className="flex h-[82vh] w-[calc(100vw-32px)] flex-col overflow-hidden gap-0 p-0 !max-w-[1160px]">
          <DialogHeader className="border-b border-border bg-surface px-5 py-4">
            <DialogTitle>知识源内容 · {sourcePreview ? sourceDisplayName(sourcePreview.path) : ""}</DialogTitle>
          </DialogHeader>
          <div className="grid min-h-0 flex-1 md:grid-cols-[300px_minmax(0,1fr)]">
            <aside className="min-h-0 overflow-auto border-b border-border bg-surface-subtle p-2 md:border-b-0 md:border-r">
              {sourcePreviewLoading ? <div className="p-3 text-xs text-muted-foreground">正在加载文件…</div> : sourceFiles.length === 0 ? (
                <div className="p-3 text-xs text-muted-foreground">该知识源暂无可查看文档。</div>
              ) : sourceFiles.map((file) => (
                <button key={file.relative_path} type="button" onClick={() => sourcePreview && loadKnowledgeSourceFile(sourcePreview, file)} className={cn("mb-1 w-full rounded-md px-3 py-2 text-left", selectedSourceFile?.relative_path === file.relative_path ? "bg-accent-soft text-accent" : "hover:bg-background")}>
                  <div className="flex min-w-0 items-center gap-2"><FileText className="size-3.5 shrink-0" /><span className="mono truncate text-[12px] font-semibold">{file.relative_path}</span></div>
                  <div className="mt-1 pl-5 text-[10.5px] text-muted-foreground">{formatBytes(file.size_bytes)} · {formatDateTime(file.updated_at)}</div>
                </button>
              ))}
              {sourceFilesTruncated && <div className="px-3 py-2 text-[10.5px] text-muted-foreground">仅显示前 1000 个文件。</div>}
            </aside>
            <section className="flex min-h-0 flex-col bg-background">
              {sourceFileLoading ? <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">正在读取文档…</div> : !selectedSourceFile ? (
                <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">从左侧选择一个文档查看内容。</div>
              ) : !sourceFileContent?.preview_available ? (
                <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-muted-foreground">{sourceFileContent?.message || "该文件暂不支持在线预览。"}</div>
              ) : <>
                <div className="flex items-center justify-between border-b border-border px-5 py-3"><span className="mono truncate text-[12px] font-semibold">{sourceFileContent.name}</span>{sourceFileContent.truncated && <Pill tone="amber">仅显示前 1 MB</Pill>}</div>
                <pre className="mono min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words bg-[#0f172a] p-5 text-[12px] leading-relaxed text-[#dbe4f0]">{sourceFileContent.content}</pre>
              </>}
            </section>
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
          className={cn(
            "flex w-full flex-col overflow-hidden p-0",
            artifactIsMarkdown ? "h-[92vh] !max-w-[1240px] gap-0" : "max-h-[88vh] !max-w-[980px] gap-4 p-4"
          )}
        >
          <DialogHeader className={cn(artifactIsMarkdown && "border-b border-border bg-surface px-5 py-3.5") }>
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
                <Button size="sm" variant="outline" onClick={() => setArtifactEditing(true)}>
                  <Pencil className="size-3.5" /> 编辑产物
                </Button>
              )}
            </div>
          </DialogHeader>
          {artifactEditing ? (
            artifactIsMarkdown ? (
              <div className="min-h-0 flex-1">
                <MarkdownWorkspace
                  value={artifactDraft}
                  onChange={setArtifactDraft}
                  onSave={saveArtifactRevision}
                  saving={artifactSaving}
                  dirty={artifactDraft !== artifactPreview?.content}
                  fileName={artifactPreview?.name}
                />
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col gap-3">
                <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800">
                  该产物使用结构化源码格式。请保留 Benchmark JSON 等文件的语法，提交进化时会再次执行完整性校验。
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
              <div className="min-h-0 flex-1 overflow-auto bg-[#f6f7f9] px-5 py-8">
                <div className="mx-auto min-h-full max-w-[820px] bg-white px-12 py-10 shadow-[0_1px_2px_rgba(15,23,42,0.04),0_14px_40px_rgba(15,23,42,0.06)]">
                  <MarkdownDocument content={artifactPreview?.content || ""} />
                </div>
              </div>
            ) : (
              <pre className="mono min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[#0f172a] p-4 text-[12px] leading-relaxed text-[#dbe4f0]">
                {artifactPreview?.content}
              </pre>
            )
          )}
        </DialogContent>
      </Dialog>

    </div>
  );
}

function isActiveJob(status: MiningJobStatus) {
  return status === "preparing" || status === "queued" || status === "running" || status === "waiting" || status === "stopping";
}

function jobStatusMeta(status: MiningJobStatus) {
  const entries: Record<MiningJobStatus, { label: string; tone: string }> = {
    preparing: { label: "准备中", tone: "gray" },
    queued: { label: "排队中", tone: "blue" },
    running: { label: "运行中", tone: "blue" },
    waiting: { label: "待知识补证", tone: "amber" },
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
  const ingestion = sourceIngestion(source);
  if (ingestion.status === "processing") return { label: "后处理中", tone: "blue" };
  if (ingestion.status === "failed") return { label: "处理失败", tone: "red" };
  if (source.ready) return { label: "可挖掘", tone: "green" };
  return { label: "空目录", tone: "amber" };
}

function sourceIngestion(source: InputSource): SourceIngestionStatus {
  return source.ingestion || {
    schema_version: 1,
    source_path: source.path,
    batch_id: "",
    status: source.ready ? "ready" : "empty",
    stage: source.ready ? "complete" : "idle",
    progress: source.ready ? 100 : 0,
    processed_files: 0,
    total_files: 0,
    current_file: "",
    error: "",
    started_at: "",
    updated_at: "",
    finished_at: "",
  };
}

function ingestionStageLabel(stage: SourceIngestionStatus["stage"]) {
  if (stage === "validating") return "校验上传文件";
  if (stage === "converting") return "转换为 Markdown";
  if (stage === "writing") return "写入知识源";
  if (stage === "merging") return "合并知识源";
  if (stage === "complete") return "处理完成";
  if (stage === "failed") return "处理失败";
  return "准备处理";
}

function jobArtifacts(job: MiningJob) {
  const source = job.artifacts?.length
    ? job.artifacts
    : (job.rounds || []).flatMap((round) => round.artifacts || []);
  const seen = new Set<string>();
  return source.filter((artifact) => {
    if (seen.has(artifact.path)) return false;
    seen.add(artifact.path);
    return true;
  });
}

function jobSkillNames(job: MiningJob) {
  return Array.from(new Set(
    jobArtifacts(job)
      .filter((artifact) => artifact.kind === "skill" && artifact.skill_name)
      .map((artifact) => artifact.skill_name)
  ));
}

function artifactTone(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "green";
  if (kind === "skill") return "purple";
  if (kind === "semantic") return "blue";
  return "gray";
}

function artifactLabel(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "Benchmark";
  if (kind === "skill") return "Skill";
  if (kind === "semantic") return "语义报告";
  return "评测定义";
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

function sourceDisplayName(path: string) {
  return path.replace(/^data[\\/]+/, "");
}

function formatRuntimeLog(line: string) {
  return line
    .replace(/HERMES_HOME/g, "模型运行配置")
    .replace(/HERMES_OK/g, "MODEL_OK")
    .replace(/\.hermes_home/g, ".model_runtime")
    .replace(/hermes/gi, "模型运行时");
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
