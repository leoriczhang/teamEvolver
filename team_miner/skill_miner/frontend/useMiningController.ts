import { miningApi as api } from "./miningApi";
import type { UserProfile } from "@/features/admin/api/client";
import { fileToB64 } from "@/lib/file";
import { createRequestId } from "@/lib/requestId";
import { toastErr,toastOk } from "@/lib/toast";
import { useEffect,useRef,useState } from "react";
import {
  candidateCanSubmit, distinctWarnings, isActiveJob, isMarkdownArtifact,
  isWaitingForEvidence, jobArtifacts, jobSkillNames, nativeRetryContext,
} from "./miningHelpers";
import type {
  ArtifactContent, CandidateSubmissionResult, EvidenceAttachment, MinePage,
  MiningArtifact, MiningJob,
} from "./types";
import { PAGE_META } from "./types";
import { useMining } from "./useMining";

export function useMiningController({
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

  return {
    active, page, onNavigate, _user,
    mining, config, rounds, setRounds,
    runtimeTimeout, setRuntimeTimeout, humanCheckpoints, setHumanCheckpoints,
    taskName, setTaskName, miningMode, setMiningMode,
    miningDirection, setMiningDirection, targetSkillCount, setTargetSkillCount,
    starting, jobFilter, setJobFilter, selectedJobId,
    setSelectedJobId, selectedJob, setSelectedJob, jobActionPending,
    deleteJob, setDeleteJob, jobDeleting, checkpointAnswers,
    setCheckpointAnswers, checkpointUnresolved, setCheckpointUnresolved, checkpointReferences,
    setCheckpointReferences, checkpointFeedback, setCheckpointFeedback, checkpointAttachments,
    checkpointUploading, checkpointSubmitting, knowledgeSupplementExpanded, setKnowledgeSupplementExpanded,
    artifactPreview, setArtifactPreview, artifactDraft, setArtifactDraft,
    artifactEditing, setArtifactEditing, artifactEditorMode, setArtifactEditorMode,
    artifactEditable, setArtifactEditable, artifactSaving, loadingArtifact,
    submittingJobId, candidateSelections, setCandidateSelections, submissionReports,
    createJobOpen, setCreateJobOpen, checkingInputSource, inputSourceCheckError,
    artifactIsMarkdown, meta, inputDir, maxRounds,
    minRounds, inputSources, skillSources, selectedSource,
    selectedSourceError, jobCounts, sourceDocumentTotal, hasUnverifiedSources,
    pipelineJobs, recentTaskCount, running, stateLabel,
    stateTone, filteredJobs, selectInputDir, verifySelectedInputSource,
    startRun, resumeJob, retryNativeJob, stopJob,
    deleteMiningJob, uploadCheckpointEvidence, submitCheckpointAnswers, previewArtifact,
    saveArtifactRevision, submitJobToEvolution,

  };
}

export type MiningController = ReturnType<typeof useMiningController>;
