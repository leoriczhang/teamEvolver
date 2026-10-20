import { Pill,type PillTone } from "@/components/common";
import { cn } from "@/lib/utils";
import {
Boxes,
Eye,
FileCode2,
FileText,
Pencil,
ScanSearch,
ShieldAlert
} from "lucide-react";

import type {
  ArtifactContent, InputSource, KnowledgeSupplementRound, MiningArtifact,
  MiningArtifactQuality, MiningArtifactQualityItem, MiningCapability, MiningJob,
  MiningJobStatus, PhaseState,
} from "./types";

export const STEP_META = [
  { key: "step1" as const, n: 1, title: "输入与提交", sub: "冻结 Evidence 并提交 Compile", icon: Boxes },
  { key: "step2" as const, n: 2, title: "OV Compile", sub: "来源解析与候选编译", icon: ScanSearch },
  { key: "step3" as const, n: 3, title: "产物复核", sub: "结构校验与 Evidence 补充", icon: FileCode2 },
];

export function TimingCard({ label, seconds, failed = false }: { label: string; seconds: number; failed?: boolean }) {
  return (
    <div className={cn("rounded-lg border px-3 py-2.5", failed ? "border-destructive/30 bg-destructive/5" : "border-border bg-surface-subtle") }>
      <div className="text-[10px] font-semibold text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 mono text-[14px] font-bold", failed && "text-destructive")}>{formatDuration(seconds)}</div>
    </div>
  );
}

export function elapsedSeconds(startedAt?: string, finishedAt?: string) {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  const finish = finishedAt ? Date.parse(finishedAt) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return null;
  return Math.max(0, (finish - start) / 1000);
}

export function nativeRetryContext(job: MiningJob) {
  const context = job.native_retry_context;
  return job.can_retry_native && context?.expected_task_id && context.format_guidance_sha256
    && Number.isFinite(context.budget_seconds) && context.budget_seconds > 0 ? context : null;
}

export function formatDuration(seconds: number) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 1) return `${value.toFixed(3)} 秒`;
  if (value < 60) return `${value.toFixed(1)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`;
}

export function isActiveJob(status: MiningJobStatus) {
  return isExecutingJob(status) || status === "waiting_human";
}

export function isExecutingJob(status: MiningJobStatus) {
  return status === "preparing" || status === "queued" || status === "running" || status === "stopping";
}

export function isWaitingForEvidence(job: MiningJob) {
  return job.status === "waiting_human";
}

export function jobStatusMeta(status: MiningJobStatus) {
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

export function jobProgress(job: MiningJob) {
  if (job.status === "queued" || job.status === "preparing") return 0;
  if (job.status === "succeeded") return 100;
  const completedRounds = Math.max(0, (job.current_round || 1) - 1);
  const doneSteps = STEP_META.filter((step) => job.phase?.[step.key] === "done").length;
  const activeStep = STEP_META.some((step) => job.phase?.[step.key] === "active") ? 0.5 : 0;
  return Math.min(98, Math.round(((completedRounds + (doneSteps + activeStep) / 3) / Math.max(1, job.max_rounds)) * 100));
}

export function pipelineStepLabel(state: PhaseState[keyof PhaseState]) {
  if (state === "active") return "进行中";
  if (state === "done") return "完成";
  return "未开始";
}

export function sourceStatusMeta(source: InputSource): { label: string; tone: PillTone } {
  if (source.status === "over_limit") return { label: "超过挖掘限额", tone: "red" };
  if (source.status === "missing") return { label: "路径已删除", tone: "red" };
  if (source.status === "unavailable") return { label: "暂不可用", tone: "amber" };
  if (source.ready) return { label: "可挖掘", tone: "green" };
  return { label: "空目录", tone: "amber" };
}

export function jobArtifacts(job: MiningJob) {
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

export function groupMiningArtifacts(artifacts: MiningArtifact[], preferredNames: string[] = []) {
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

export function qualityLabel(quality: MiningArtifactQuality) {
  return quality.level === "ready" && quality.confidence !== "未验收" ? "结构校验通过" : quality.label;
}

export function CapabilityDetails({ capability }: { capability: MiningCapability }) {
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

export function MiningArtifactFiles({ artifacts, editable, historical = false, loadingPath, onPreview }: {
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

export function ArchivedArtifactGroups({ artifacts, round, loadingPath, onPreview }: {
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

export function jobSkillNames(job: MiningJob) {
  return Array.from(new Set(
    [
      ...jobArtifacts(job)
        .filter((artifact) => artifact.kind === "skill" && artifact.skill_name)
        .map((artifact) => artifact.skill_name),
      ...(job.candidate_contract_version === 2 ? job.capability_plan?.skills.map((skill) => skill.name) || [] : []),
    ]
  ));
}

export function candidateCanSubmit(job: MiningJob, skillName: string) {
  const quality = job.candidate_quality?.[skillName];
  const candidateReady = job.candidate_contract_version === 2
    ? quality?.can_submit === true && quality.has_skill
    : quality?.can_submit !== false;
  return job.status === "succeeded"
    && job.artifact_quality?.can_submit !== false
    && candidateReady;
}

export function artifactTone(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "green";
  if (kind === "skill") return "purple";
  if (kind === "semantic") return "blue";
  return "gray";
}

export function artifactQualityTone(state: MiningArtifactQuality["level"] | MiningArtifactQualityItem["state"]): PillTone {
  if (state === "ready") return "green";
  if (state === "incomplete") return "red";
  return "amber";
}

export function artifactLabel(kind: MiningArtifact["kind"]) {
  if (kind === "benchmark") return "Test Dataset";
  if (kind === "skill") return "Skill";
  if (kind === "semantic") return "语义报告";
  if (kind === "evaluation") return "评测定义";
  if (kind === "resource") return "配套资源";
  return "文件";
}

export function distinctWarnings(warnings?: string[]) {
  return Array.from(new Set((warnings || []).filter((warning) => typeof warning === "string" && warning.trim())));
}

export function pipelineWarningMessages(errors: unknown): string[] {
  const values = Array.isArray(errors) ? errors.slice(0, 20) : [errors];
  return distinctWarnings(values.map((value) => {
    if (typeof value === "string") return value.slice(0, 4000);
    if (!value || typeof value !== "object") return "";
    const error = value as Record<string, unknown>;
    return ["stage", "code", "message", "error"].map((key) => error[key]).filter((part) => typeof part === "string").join(" · ").slice(0, 4000);
  }));
}

export function compileIssueKinds(messages: string[]) {
  return {
    reduceMergeTruncated: messages.some((message) => /\breduce_merge\b\s*(?::|·)[\s\S]{0,300}(?:truncat|output\s+limit\s+reached)/i.test(message)),
    reduceTruncated: messages.some((message) => /\breduce\b\s*(?::|·|output\b)[\s\S]{0,300}(?:truncat|output\s+limit\s+reached)/i.test(message)),
    sourceIncomplete: messages.some((message) => /\bextract\s*(?::|·)|\bextract(?:ion)?(?:\s+(?:worker|job|step|stage))?\s+(?:failed|failure|incomplete)\b|\bsources?\s+(?:(?:processing|reading|extraction)\s+)?(?:failed|incomplete)\b|\bfailed\s+to\s+(?:read|process|extract)\s+(?:the\s+)?source\b|来源(?:读取|处理)?(?:失败|未完整处理)/i.test(message)),
  };
}

export function RemoteCompileWarnings({ warnings, label, historical = false }: { warnings: string[]; label: string; historical?: boolean }) {
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

export function KnowledgeSupplementHistory({ rounds }: { rounds: KnowledgeSupplementRound[] }) {
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

export function isMarkdownArtifact(artifact: Pick<ArtifactContent, "name" | "path"> | null) {
  if (!artifact) return false;
  return /\.(?:md|markdown)$/i.test(artifact.name) || /\.(?:md|markdown)$/i.test(artifact.path);
}

export function formatDateTime(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
