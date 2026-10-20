import { PageHeader,Panel,Pill,StatCard } from "@/components/common";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { Button } from "@/components/ui/button";
import { Dialog,DialogContent,DialogHeader,DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
ChevronDown,
Eye,
FolderPlus,
ListChecks,
Pencil,
Play,
RotateCcw,
Save,
Trash2,
Upload
} from "lucide-react";
import { markdownBodyForPreview } from "./markdownFrontmatter";

import MiningWhiteboxPanel from "@skill-miner/MiningWhiteboxPanel";
import SkillSourceWorkspace from "@skill-miner/SkillSourceWorkspace";
import { STEP_META,formatBytes,formatDateTime,jobStatusMeta,pipelineStepLabel,sourceStatusMeta } from "./miningHelpers";
import MiningJobDetail from "./MiningJobDetail";
import { useMiningController } from "./useMiningController";
export type { MinePage } from "./types";

export default function MiningView(props: Parameters<typeof useMiningController>[0]) {
  const controller = useMiningController(props);
  const {
    active, page, onNavigate, _user,
    mining, config, rounds, setRounds,
    runtimeTimeout, setRuntimeTimeout, humanCheckpoints, setHumanCheckpoints,
    taskName, setTaskName, miningMode, setMiningMode,
    miningDirection, setMiningDirection, targetSkillCount, setTargetSkillCount,
    starting, jobFilter, setJobFilter, selectedJobId,
    setSelectedJobId, selectedJob, setSelectedJob, deleteJob,
    setDeleteJob, jobDeleting, artifactPreview, setArtifactPreview,
    artifactDraft, setArtifactDraft, artifactEditing, setArtifactEditing,
    artifactEditorMode, setArtifactEditorMode, artifactEditable, setArtifactEditable,
    artifactSaving, createJobOpen, setCreateJobOpen, checkingInputSource,
    inputSourceCheckError, artifactIsMarkdown, meta, inputDir,
    maxRounds, minRounds, inputSources, skillSources,
    selectedSource, selectedSourceError, jobCounts, sourceDocumentTotal,
    hasUnverifiedSources, pipelineJobs, recentTaskCount, running,
    stateLabel, stateTone, filteredJobs, selectInputDir,
    verifySelectedInputSource, startRun, deleteMiningJob, saveArtifactRevision,
  } = controller;
  if (!props.active) return null;

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
                          ? <MiningJobDetail job={selectedJob} controller={controller} />
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
