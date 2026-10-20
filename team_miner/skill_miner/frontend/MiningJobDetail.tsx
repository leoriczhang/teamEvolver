import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
ArrowRight,
CheckCircle2,
ChevronDown,
CircleStop,
Clock3,
ListChecks,
Send,
ShieldAlert,
Trash2
} from "lucide-react";

import {
  ArchivedArtifactGroups, CapabilityDetails, KnowledgeSupplementHistory, MiningArtifactFiles,
  RemoteCompileWarnings, STEP_META, TimingCard, artifactQualityTone,
  candidateCanSubmit, compileIssueKinds, distinctWarnings, elapsedSeconds,
  formatDateTime, formatDuration, groupMiningArtifacts, isActiveJob,
  isExecutingJob, isWaitingForEvidence, jobArtifacts, jobProgress,
  jobSkillNames, jobStatusMeta, nativeRetryContext, pipelineWarningMessages,
  qualityLabel,
} from "./miningHelpers";
import type { MiningJob } from "./types";
import type { MiningController } from "./useMiningController";

export default function MiningJobDetail({ job, controller }: { job: MiningJob; controller: MiningController }) {
  const {
    active, onNavigate, mining, rounds,
    jobActionPending, setDeleteJob, checkpointAnswers, setCheckpointAnswers,
    checkpointUnresolved, setCheckpointUnresolved, checkpointReferences, setCheckpointReferences,
    checkpointFeedback, setCheckpointFeedback, checkpointAttachments, checkpointUploading,
    checkpointSubmitting, knowledgeSupplementExpanded, setKnowledgeSupplementExpanded, loadingArtifact,
    submittingJobId, candidateSelections, setCandidateSelections, submissionReports,
    resumeJob, retryNativeJob, stopJob, uploadCheckpointEvidence,
    submitCheckpointAnswers, previewArtifact, submitJobToEvolution,
  } = controller;
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
            <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">{job.artifact_quality.confidence === "未验收"
              ? "当前产物仅完成挖掘，未执行内容验收，不能提交 Candidate Review。"
              : "“需复核”表示仍有内容需要人工核对。结构校验通过仅确认产物格式，业务正确性还需 True Replay 和 Candidate Review 验证。"}</p>
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
