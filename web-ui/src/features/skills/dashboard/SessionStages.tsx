import { api } from "@/api/client";
import { Pill, type PillTone } from "@/components/common";
import type { SessionStage, SessionStageDetail } from "@/features/sessions/api/client";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Check, ChevronDown, CircleAlert, Clock3, Loader2 } from "lucide-react";
import { useEffect, useId, useState } from "react";

const STAGES: Record<string, { label: string; description: string }> = {
  normalize: { label: "标准化", description: "整理原始会话，统一轮次与工具记录。" },
  split: { label: "会话切分", description: "识别任务边界，确认需要独立分析的会话。" },
  analyze: { label: "会话分析", description: "判断会话价值，评估任务完成、回答质量与工具使用。" },
  trace_analyze: { label: "Trace 分析", description: "逐条检查主、子 Agent 的执行步骤与问题。" },
  archive: { label: "归档", description: "保存通过必要分析阶段的会话及其结果。" },
};
const ORDER = Object.keys(STAGES);
const STATUSES: Record<string, { label: string; tone: PillTone }> = {
  succeeded: { label: "已完成", tone: "green" },
  failed: { label: "失败", tone: "red" },
  running: { label: "执行中", tone: "blue" },
  processing: { label: "处理中", tone: "blue" },
  queued: { label: "排队中", tone: "amber" },
  retry_wait: { label: "等待重试", tone: "amber" },
  skipped: { label: "已跳过", tone: "gray" },
  cancelled: { label: "已取消", tone: "gray" },
  waiting_external: { label: "等待外部结果", tone: "amber" },
};
const LABELS: Record<string, string> = {
  overview: "会话概况", summary: "分析摘要", value_judge: "价值判断", judge: "评审评分",
  decision: "结论", confidence: "置信度", reason: "判断依据", rationale: "评分依据",
  overall_score: "综合评分", task_completion: "任务完成", response_quality: "回答质量",
  efficiency: "效率", tool_usage: "工具使用", reasons: "各维度依据", model: "分析模型", mode: "分析方式",
  session_id: "会话 ID", user_id: "用户 ID", turn_count: "轮数", tool_call_count: "工具调用数",
  timestamp: "业务时间", business_time: "业务时间", analyzed_at: "分析时间",
  segments: "切分结果", segment_count: "会话数", split: "是否切分", children: "子会话",
  missing: "缺失阶段", title: "任务标题", traces: "Trace 结果", trace_id: "Trace ID",
  turn_num: "轮次", is_badcase: "存在问题", problem_type: "问题类型", problem_type_label: "问题类型",
  problem_description: "问题说明", agent_name: "Agent", session_thread_id: "线程 ID",
  parent_trace_id: "父 Trace ID", parent_turn_num: "父轮次", used_skills: "使用的 Skill",
};
const VALUES: Record<string, string> = {
  valuable: "有价值会话", chitchat: "闲聊", task_only: "仅当前任务", memory_candidate: "个人记忆候选",
  skipped: "跳过", model: "模型分析", merged: "联合分析",
  normalize: "标准化", split: "会话切分", analyze: "会话分析", trace_analyze: "Trace 分析", archive: "归档",
};

function Value({ value, field = "" }: { value: unknown; field?: string }) {
  if (value === null || value === undefined || value === "") return <span className="text-muted-foreground">—</span>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "number") {
    const text = field === "confidence" ? `${Math.round(value * 100)}%`
      : field.endsWith("score") || ["task_completion", "response_quality", "efficiency", "tool_usage"].includes(field)
        ? value.toFixed(2) : value.toLocaleString("zh-CN");
    return <span className="font-mono tabular-nums">{text}</span>;
  }
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-muted-foreground">无</span>;
    return <ul className="space-y-2">{value.map((item, index) => {
      const trace = field === "traces" && item && typeof item === "object"
        ? item as Record<string, unknown> : null;
      return <li key={index} className={cn("min-w-0", typeof item === "object" && item !== null && "rounded-lg border border-border bg-background p-3")}>
        {trace && <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold">Trace {index + 1}</span>
          <Pill tone={trace.is_badcase === true ? "red" : trace.is_badcase === false ? "green" : "gray"}>
            {trace.is_badcase === true ? "发现问题" : trace.is_badcase === false ? "未发现问题" : "未判定"}
          </Pill>
        </div>}
        <Value value={item} />
      </li>;
    })}</ul>;
  }
  if (typeof value === "object") {
    return <dl className="space-y-3">{Object.entries(value as Record<string, unknown>).map(([key, item]) => (
      <div key={key} className="min-w-0 grid gap-1 sm:grid-cols-[110px_minmax(0,1fr)] sm:gap-4">
        <dt className="text-xs text-muted-foreground">{LABELS[key] || key}</dt>
        <dd className="min-w-0 text-xs leading-relaxed [overflow-wrap:anywhere]"><Value value={item} field={key} /></dd>
      </div>
    ))}</dl>;
  }
  const text = String(value);
  return <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">
    {field.endsWith("_at") || field === "timestamp" || field === "business_time" ? fmtTime(text) : VALUES[text] || text}
  </span>;
}

function StageItem({ sid, stage, index, last }: { sid: string; stage: SessionStage; index: number; last: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [data, setData] = useState<SessionStageDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const contentId = useId();
  const meta = STAGES[stage.stage] || { label: stage.stage, description: "查看该阶段的执行记录。" };
  const status = STATUSES[stage.status] || { label: stage.status, tone: "gray" as const };

  useEffect(() => {
    if (!expanded || data) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<SessionStageDetail>(
      `/api/sessions/${encodeURIComponent(sid)}/stages/${encodeURIComponent(stage.stage)}`,
      { signal: controller.signal },
    ).then(value => {
      if (!controller.signal.aborted) { setData(value); setLoading(false); }
    }).catch(e => {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : "读取失败");
        setLoading(false);
      }
    });
    return () => controller.abort();
  }, [sid, stage.stage, expanded, data, retry]);

  const running = ["running", "processing"].includes(stage.status);
  const failed = stage.status === "failed";
  return <li className="relative grid min-w-0 grid-cols-[32px_minmax(0,1fr)] gap-3 pb-3 last:pb-0 sm:gap-4">
    {!last && <span aria-hidden="true" className="absolute bottom-0 left-[15px] top-9 w-px bg-border" />}
    <span aria-hidden="true" className={cn("z-10 mt-3 flex size-8 items-center justify-center rounded-full border text-xs font-semibold",
      stage.status === "succeeded" ? "border-green-600/20 bg-green-600/10 text-green-700 dark:text-green-400"
        : failed ? "border-destructive/30 bg-destructive/10 text-destructive" : "border-border bg-muted text-muted-foreground")}>
      {stage.status === "succeeded" ? <Check className="size-4" /> : failed ? <CircleAlert className="size-4" />
        : running ? <Loader2 className="size-4 motion-safe:animate-spin" /> : index + 1}
    </span>
    <div className={cn("min-w-0 overflow-hidden rounded-xl border bg-background", expanded ? "border-accent/40" : "border-border", failed && "border-destructive/30")}>
      <button type="button" aria-expanded={expanded} aria-controls={contentId}
        aria-label={`${expanded ? "收起" : "展开"}${meta.label}阶段`}
        onClick={() => setExpanded(value => !value)}
        className="flex w-full min-w-0 items-center gap-3 p-4 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold">{meta.label}</span><Pill tone={status.tone}>{status.label}</Pill></div>
          <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{meta.description}</p>
          <span className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <Clock3 aria-hidden="true" className="size-3" />更新于 {fmtTime(stage.updated_at || stage.created_at)}
          </span>
        </div>
        <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:block">{expanded ? "收起详情" : "查看详情"}</span>
        <ChevronDown aria-hidden="true" className={cn("size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none", expanded && "rotate-180")} />
      </button>
      {expanded && <div id={contentId} role="region" aria-label={`${meta.label}详情`} className="space-y-4 border-t border-border bg-muted/20 p-4">
        <dl className="grid gap-3 text-xs sm:grid-cols-3">
          <div><dt className="text-muted-foreground">阶段标识</dt><dd className="mt-1 font-mono">{stage.stage}</dd></div>
          <div><dt className="text-muted-foreground">记录时间</dt><dd className="mt-1">{fmtTime(stage.created_at)}</dd></div>
          <div><dt className="text-muted-foreground">更新时间</dt><dd className="mt-1">{fmtTime(stage.updated_at)}</dd></div>
        </dl>
        {(data?.error_code || stage.error_code) && <p className="rounded-lg border border-destructive/20 bg-destructive/5 p-3 text-xs text-destructive [overflow-wrap:anywhere]">
          错误代码：{data?.error_code || stage.error_code}
        </p>}
        {loading ? <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3.5 motion-safe:animate-spin" />正在读取阶段详情…</p>
          : error ? <div role="alert" className="text-xs text-destructive">详情加载失败：{error}
            <button type="button" onClick={() => setRetry(value => value + 1)} className="ml-3 font-semibold underline">重试</button></div>
          : data && <>
            {!!Object.keys(data.detail || {}).length && <div className="rounded-lg border border-border bg-background p-3">
              <h4 className="mb-3 text-xs font-semibold">执行记录</h4><Value value={data.detail} />
            </div>}
            {data.result_available && data.result ? <div className="min-w-0 space-y-3">
              <h4 className="text-xs font-semibold">阶段结果</h4><Value value={data.result} />
            </div> : <p className="text-xs text-muted-foreground">{data.result_unavailable_reason || "暂无阶段产物"}</p>}
          </>}
      </div>}
    </div>
  </li>;
}

export default function SessionStages({ sid, stages }: { sid: string; stages: SessionStage[] }) {
  const ordered = [...stages].sort((a, b) => {
    const rank = (name: string) => ORDER.includes(name) ? ORDER.indexOf(name) : ORDER.length;
    return rank(a.stage) - rank(b.stage);
  });
  const completed = stages.filter(stage => stage.status === "succeeded").length;
  const failed = stages.filter(stage => stage.status === "failed").length;
  return <section aria-label="会话处理阶段" className="min-w-0 space-y-5">
    <header className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-muted/30 px-4 py-3">
      <div><h3 className="text-sm font-semibold">处理时间线</h3><p className="mt-1 text-xs text-muted-foreground">按处理顺序展示，点击阶段查看执行详情。</p></div>
      <div className="flex items-center gap-2 text-xs"><Pill tone="green">已完成 {completed} / {stages.length}</Pill>
        {failed > 0 && <Pill tone="red">失败 {failed}</Pill>}</div>
    </header>
    <ol className="min-w-0">{ordered.map((stage, index) => <StageItem
      key={`${sid}:${stage.stage}:${stage.updated_at || ""}`} sid={sid} stage={stage} index={index} last={index === ordered.length - 1} />)}</ol>
  </section>;
}
