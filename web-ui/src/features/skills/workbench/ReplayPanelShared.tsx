import {
Empty,
ListViewport,
PaginationControls,
Panel,
Pill,
StatCard,
usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { type WorkbenchBranch,type WorkbenchDataset,type WorkbenchMaterial,type WorkbenchRun,type WorkbenchTraceMessage } from "@/features/replay/api/client";
import { isActiveRun } from "@/features/replay/api/datasets";
import { cn } from "@/lib/utils";
import {
Bot,
MessageSquare,
Plus,
RefreshCw,
Terminal,
Trash2,
User,
Wrench
} from "lucide-react";
import { type ReactNode } from "react";

export type DatasetDraft = {
  dataset_id?: string;
  skill_ids: string[];
  name: string;
  query: string;
  outputChecks: string;
  trajectoryChecks: string;
  disclosure_batch_size: number;
  enabled_for_evolution: boolean;
};

export const EMPTY_DATASET: DatasetDraft = {
  skill_ids: [],
  name: "",
  query: "",
  outputChecks: "",
  trajectoryChecks: "",
  disclosure_batch_size: 4,
  enabled_for_evolution: false,
};

export const METRICS = [
  { key: "interaction_turns", label: "轮次" },
  { key: "tool_call_count", label: "Tool 调用" },
  { key: "total_tokens", label: "Tokens" },
] as const;

export const primaryCase = (dataset?: WorkbenchDataset | null) => dataset?.cases?.[0];

export const checkText = (dataset: WorkbenchDataset, kind: "output" | "trajectory") =>
  (primaryCase(dataset)?.checks || [])
    .filter((check) => check.kind === kind)
    .map((check) => check.text)
    .join("\n");

export const datasetMaterials = (dataset: WorkbenchDataset) => primaryCase(dataset)?.materials || [];

export const datasetSkillIds = (dataset: WorkbenchDataset) => dataset.skills.map((skill) => skill.skill_id);

export const datasetReadOnly = (dataset: WorkbenchDataset) => !!dataset.metadata?.read_only;

export const datasetForEvolution = (dataset: WorkbenchDataset) => !!dataset.metadata?.enabled_for_evolution;

export function DatasetDetail({ dataset }: { dataset: WorkbenchDataset }) {
  const datasetCase = primaryCase(dataset);
  const requirements = (datasetCase?.checks || []).filter((check) => check.kind === "output").map((check) => check.text);
  const trajectory = (datasetCase?.checks || []).filter((check) => check.kind === "trajectory").map((check) => check.text);
  const materialIntegrity = dataset.material_integrity;
  const sourceSessionIds = Array.from(new Set([
    ...(datasetCase?.provenance.session_ids || []),
    ...(datasetCase?.provenance.session_id ? [datasetCase.provenance.session_id] : []),
  ]));
  return (
    <div
      className="mt-4 space-y-4 rounded-lg border border-border bg-surface p-4"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-bold">数据集详情</div>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone="blue">初始 Query</Pill>
          <Pill tone="amber">{requirements.length} 条 Checklist</Pill>
          <Pill tone="purple">{trajectory.length} 条轨迹要求</Pill>
          {!!datasetSkillIds(dataset).length && (
            <Pill tone="gray">Skills: {datasetSkillIds(dataset).join("、")}</Pill>
          )}
          {dataset.cases.length > 1 && (
            <Pill tone="gray">{dataset.cases.length} 个任务</Pill>
          )}
          {datasetForEvolution(dataset) && <Pill tone="amber">固定回归集</Pill>}
          {materialIntegrity?.status === "missing" ? (
            <Pill tone="red">材料不完整</Pill>
          ) : materialIntegrity?.mode === "inline" ? (
            <Pill tone="green">内嵌材料</Pill>
          ) : materialIntegrity?.status === "complete" ? (
            <Pill tone="green">材料齐全</Pill>
          ) : null}
          <Pill tone="gray">
            每轮披露 {datasetCase?.replay.progressive_disclosure?.batch_size || 4} 条
          </Pill>
        </div>
      </div>

      <DatasetSection title="初始 Query">
        <pre className="mono whitespace-pre-wrap break-words text-[12px] leading-relaxed">
          {datasetCase?.query || "（空）"}
        </pre>
      </DatasetSection>

      <DatasetSection title={`要求 Checklist（${requirements.length}）`}>
        <PagedTextWindow
          items={requirements}
          empty="未定义 Checklist，该条目不会作为正式渐进数据集运行。"
        />
      </DatasetSection>

      <DatasetSection title={`轨迹要求（${trajectory.length}）`}>
        <PagedTextWindow items={trajectory} empty="无额外轨迹要求。" />
      </DatasetSection>

      <DatasetSection title={`相关材料（${datasetMaterials(dataset).length}）`}>
        {materialIntegrity?.status === "missing" ? (
          <div className="space-y-2">
            <div className="text-xs font-semibold text-destructive">
              缺少回放所需材料，运行前必须补齐。
            </div>
            {(materialIntegrity.missing_paths || []).map((path) => (
              <div key={path} className="mono rounded border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-xs text-destructive">
                {path}
              </div>
            ))}
          </div>
        ) : datasetMaterials(dataset).length ? (
          <div className="space-y-1.5">
            {datasetMaterials(dataset).map((material) => (
              <div key={material.path} className="flex items-center justify-between gap-3 rounded border border-border px-2.5 py-2 text-xs">
                <span className="mono break-all">{material.path}</span>
                <span className="shrink-0 text-muted-foreground">{formatBytes(material.size || 0)}</span>
              </div>
            ))}
          </div>
        ) : materialIntegrity?.mode === "inline" ? (
          <pre className="mono max-h-[280px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-success/30 bg-success/5 p-3 text-[11px] leading-relaxed">
            {inlineMaterialText(datasetCase?.query || "") || "材料已直接写入初始 Query。"}
          </pre>
        ) : sourceSessionIds.length ? (
          <span className="text-xs text-muted-foreground">
            当前 Query 不依赖外部输入文件；来源 Session 仅用于追溯和运行时上下文。
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">无相关材料。</span>
        )}
      </DatasetSection>

      <div className="grid gap-3 text-[11px] text-muted-foreground md:grid-cols-2">
        <div>
          <span className="font-semibold">来源 Session：</span>
          <span className="mono break-all">{sourceSessionIds.join(", ") || "—"}</span>
        </div>
        <div>
          <span className="font-semibold">来源 Job：</span>
          <span className="mono break-all">{dataset.source?.job_id || dataset.source?.candidate_job_id || "—"}</span>
        </div>
      </div>

      <details>
        <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
          查看标准 Markdown
        </summary>
        <pre className="mono mt-2 max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-background p-3 text-[11px] leading-relaxed">
          {dataset.dataset_markdown || "（空）"}
        </pre>
      </details>
    </div>
  );
}

export function ChecklistEditor({
  label,
  hint,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  hint: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const items = editableChecklistLines(value);

  function commit(next: string[]) {
    onChange(
      next
        .map((item, index) => `${index + 1}. ${item.replace(/\r?\n+/g, " ")}`)
        .join("\n")
    );
  }

  function update(index: number, text: string) {
    const next = [...items];
    next[index] = text;
    commit(next);
  }

  function add(afterIndex: number = items.length - 1) {
    const next = [...items];
    next.splice(afterIndex + 1, 0, "");
    commit(next);
  }

  function remove(index: number) {
    commit(items.filter((_, itemIndex) => itemIndex !== index));
  }

  return (
    <section>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div>
          <div className="text-xs font-semibold text-muted-foreground">
            {label}（{items.filter((item) => item.trim()).length}）
          </div>
          <div className="mt-1 text-[11px] text-muted-soft">{hint}</div>
        </div>
        <Button variant="outline" size="sm" type="button" onClick={() => add()}>
          <Plus className="size-3.5" /> 新增一条
        </Button>
      </div>
      <ListViewport maxHeight="310px">
        <div className="space-y-2 rounded-lg border border-border bg-background p-2.5">
          {items.length ? items.map((item, index) => (
            <div
              key={index}
              className="flex items-start gap-2 rounded-md border border-line bg-surface p-2"
            >
              <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted text-[11px] font-bold text-muted-foreground">
                {index + 1}
              </span>
              <Textarea
                rows={2}
                value={item}
                aria-label={`${label}第 ${index + 1} 条`}
                placeholder={placeholder}
                className="min-h-[58px] flex-1 resize-none [field-sizing:fixed] text-xs leading-relaxed"
                onChange={(event) => update(index, event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    add(index);
                  }
                }}
              />
              <Button
                variant="ghost"
                size="icon-sm"
                type="button"
                aria-label={`删除${label}第 ${index + 1} 条`}
                className="shrink-0 text-destructive hover:text-destructive"
                onClick={() => remove(index)}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          )) : (
            <div className="px-3 py-5 text-center text-xs text-muted-foreground">
              暂无条目，点击“新增一条”开始填写。
            </div>
          )}
        </div>
      </ListViewport>
    </section>
  );
}

export function DatasetSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">{title}</div>
      <div className="rounded-md border border-border bg-background p-3">{children}</div>
    </section>
  );
}

export function PagedTextWindow({ items, empty }: { items: string[]; empty: string }) {
  const pager = usePagedItems(items, 5, 7);
  if (!items.length) {
    return <span className="text-xs text-muted-foreground">{empty}</span>;
  }
  return (
    <div className="-m-3">
      <ListViewport maxHeight="190px">
        <div className="min-h-[150px] space-y-2 p-3">
          {pager.items.map((item, index) => (
            <div
              key={`${pager.start + index}-${item}`}
              className="flex items-start gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 text-xs leading-relaxed"
            >
              <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted text-[11px] font-bold text-muted-foreground">
                {pager.start + index + 1}
              </span>
              <span className="min-w-0 flex-1">{item}</span>
            </div>
          ))}
        </div>
      </ListViewport>
      <PaginationControls
        {...pager}
        visiblePages={7}
        onPageChange={pager.setPage}
      />
    </div>
  );
}

export function RunDetail({ run, onRefresh }: { run: WorkbenchRun; onRefresh: () => void }) {
  const result = run.result;
  const dimensions = result?.efficiency?.dimensions || run.result_summary?.efficiency?.dimensions || {};
  const replayCase = result?.cases?.[0];
  return (
    <div id="workbench-run-detail">
      <Panel
        title={
          <span className="flex flex-wrap items-center gap-2">
            完整 Trace · <span className="mono text-xs">{run.run_id}</span>
            <RunStatus run={run} />
          </span>
        }
        extra={
          <Button variant="outline" size="sm" onClick={onRefresh}>
            <RefreshCw className="size-3.5" /> 刷新
          </Button>
        }
      >
        {isActiveRun(run.status) ? (
          <Empty>True Replay 正在并行执行 baseline / candidate 分支，完成后自动显示完整 Trace。</Empty>
        ) : !result ? (
          <Empty>运行结果尚未写入。</Empty>
        ) : (
          <div className="space-y-5 p-4">
            <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3.5">
              {METRICS.map((metric) => (
                <MetricCard
                  key={metric.key}
                  label={metric.label}
                  metric={dimensions[metric.key]}
                />
              ))}
              <StatCard label="客观结论" value={<Verdict value={result.verdict} />} />
              <StatCard label="模型" value={result.harness?.model || "—"} mono />
            </div>
            {result.reason && (
              <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
                {result.reason}
              </div>
            )}
            <div className="grid gap-5 2xl:grid-cols-2">
              <TraceBranch title="Baseline · 当前 Skill" branch={replayCase?.baseline} tone="gray" />
              <TraceBranch title="Candidate · 实验草稿" branch={replayCase?.candidate} tone="blue" />
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}

export function TraceBranch({
  title,
  branch,
  tone,
}: {
  title: string;
  branch?: WorkbenchBranch;
  tone: "gray" | "blue";
}) {
  const messages = branch?.messages || [];
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-background/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface-subtle px-4 py-3">
        <div className="flex items-center gap-2 font-semibold">
          <Terminal className="size-4" /> {title}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone={tone}>{branch?.interaction_turns ?? 0} 轮</Pill>
          <Pill tone={tone}>{branch?.tool_call_count ?? 0} tools</Pill>
          <Pill tone={tone}>{Number(branch?.total_tokens || 0).toLocaleString()} tokens</Pill>
          <Pill tone={branch?.ok ? "green" : "red"}>{branch?.ok ? "完成" : "失败"}</Pill>
        </div>
      </div>
      {branch?.error && (
        <pre className="mono whitespace-pre-wrap break-words border-b border-line bg-red-50 p-3 text-xs text-red-700">
          {branch.error}
        </pre>
      )}
      {!!branch?.interactions?.length && (
        <div className="space-y-2 border-b border-line p-3">
          <div className="text-xs font-semibold text-muted-foreground">
            渐进式 Checklist 交互
          </div>
          {branch.interactions.map((interaction, index) => {
            const report = interaction.checklist_report as WorkbenchBranch["checklist_report"] | undefined;
            return (
              <details
                key={index}
                className="overflow-hidden rounded-md border border-border bg-surface"
                open={index === branch.interactions!.length - 1}
              >
                <summary className="flex cursor-pointer flex-wrap items-center gap-2 bg-surface-subtle px-3 py-2 text-xs font-semibold">
                  第 {Number(interaction.interaction_num || index + 1)} 轮
                  <Pill tone={report?.all_satisfied ? "green" : "amber"}>
                    {report?.satisfied_count ?? 0}/{report?.total ?? 0} 已满足
                  </Pill>
                  <span className="text-muted-foreground">
                    {Number(interaction.tool_call_count || 0)} tools · {Number(interaction.total_tokens || 0).toLocaleString()} tokens
                  </span>
                </summary>
                <div className="space-y-3 border-t border-line p-3">
                  <TraceBlock label="本轮披露 / 用户消息" value={String(interaction.prompt || "")} />
                  <TraceBlock label="Agent 回复" value={String(interaction.response || "")} />
                  {!!report?.items?.length && (
                    <div>
                      <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">Checklist 判定</div>
                      <div className="space-y-1">
                        {report.items.map((item) => (
                          <div key={item.id} className="flex items-start gap-2 rounded border border-border px-2.5 py-2 text-[11px]">
                            <Pill tone={item.satisfied ? "green" : "amber"}>
                              {item.satisfied ? "满足" : "未满足"}
                            </Pill>
                            <div className="min-w-0">
                              <div><span className="mono text-muted-soft">{item.id}</span> {item.text}</div>
                              {item.evidence && <div className="mt-1 text-muted-foreground">{item.evidence}</div>}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </details>
            );
          })}
        </div>
      )}
      <div className="max-h-[760px] space-y-2 overflow-auto p-3">
        {messages.length ? messages.map((message, index) => (
          <TraceMessage key={`${message.role}-${index}`} message={message} index={index} />
        )) : branch?.trajectory ? (
          <pre className="mono whitespace-pre-wrap break-words text-[11px] leading-relaxed">
            {branch.trajectory}
          </pre>
        ) : (
          <Empty>该分支没有可展示的消息。</Empty>
        )}
      </div>
      <details className="border-t border-line" open>
        <summary className="cursor-pointer bg-surface-subtle px-4 py-2.5 text-xs font-semibold">
          最终输出
        </summary>
        <pre className="mono max-h-[420px] overflow-auto whitespace-pre-wrap break-words p-4 text-[11px] leading-relaxed">
          {branch?.final_response || "（空）"}
        </pre>
      </details>
    </section>
  );
}

export function TraceBlock({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold text-muted-foreground">{label}</div>
      <pre className="mono max-h-[260px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-background p-2.5 text-[11px] leading-relaxed">
        {value || "（空）"}
      </pre>
    </div>
  );
}

export function TraceMessage({ message, index }: { message: WorkbenchTraceMessage; index: number }) {
  const role = String(message.role || "unknown");
  const icon = role === "user"
    ? <User className="size-3.5" />
    : role === "assistant"
      ? <Bot className="size-3.5" />
      : role === "tool"
        ? <Wrench className="size-3.5" />
        : <MessageSquare className="size-3.5" />;
  const tone = role === "assistant" ? "blue" : role === "tool" ? "purple" : role === "user" ? "green" : "gray";
  const toolCalls = message.tool_calls || [];
  const content = formatContent(message.content);
  return (
    <details className="overflow-hidden rounded-md border border-border bg-surface" open={role === "tool" || !!toolCalls.length}>
      <summary className="flex cursor-pointer items-center gap-2 bg-surface-subtle px-3 py-2 text-xs font-semibold">
        {icon}
        <Pill tone={tone}>{role}</Pill>
        <span>#{index + 1}</span>
        {message.name && <span className="mono text-muted-foreground">{message.name}</span>}
        {message.tool_call_id && <span className="mono truncate text-[10px] text-muted-soft">{message.tool_call_id}</span>}
      </summary>
      {content && (
        <pre className="mono overflow-auto whitespace-pre-wrap break-words border-t border-line p-3 text-[11px] leading-relaxed">
          {content}
        </pre>
      )}
      {toolCalls.map((call, callIndex) => (
        <div key={call.id || callIndex} className="border-t border-line p-3">
          <div className="mb-1 flex items-center gap-2 text-xs font-semibold">
            <Wrench className="size-3.5" />
            <span className="mono">{call.function?.name || "tool"}</span>
            {call.id && <span className="mono text-[10px] text-muted-soft">{call.id}</span>}
          </div>
          <pre className="mono max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-[#0f172a] p-3 text-[11px] leading-relaxed text-[#cbd5e1]">
            {formatArguments(call.function?.arguments)}
          </pre>
        </div>
      ))}
    </details>
  );
}

export function MetricCard({
  label,
  metric,
}: {
  label: string;
  metric?: {
    baseline?: number;
    candidate?: number;
    delta?: number;
    reduction_ratio?: number;
    winner?: string;
  };
}) {
  if (!metric || metric.baseline == null || metric.candidate == null) return (
    <div className="rounded-lg border border-border p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-2">指标不可用</div>
    </div>
  );
  const delta = Number(metric?.delta || 0);
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-soft)]">
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{label}</div>
      <div className="flex items-baseline gap-2 text-lg font-bold">
        <span>{Number(metric?.baseline || 0).toLocaleString()}</span>
        <span className="text-muted-soft">→</span>
        <span>{Number(metric?.candidate || 0).toLocaleString()}</span>
      </div>
      <div className={cn(
        "mt-1 text-xs font-semibold",
        delta > 0 ? "text-success" : delta < 0 ? "text-destructive" : "text-muted-foreground"
      )}>
        {delta > 0 ? `减少 ${delta.toLocaleString()}` : delta < 0 ? `增加 ${Math.abs(delta).toLocaleString()}` : "持平"}
      </div>
    </div>
  );
}

export function MetricInline({ value }: { value?: { baseline?: number; candidate?: number; delta?: number } }) {
  if (!value || value.baseline == null || value.candidate == null) return <span className="text-muted-foreground">—</span>;
  const delta = Number(value.delta || 0);
  return (
    <span className={cn(
      "mono text-xs",
      delta > 0 ? "text-success" : delta < 0 ? "text-destructive" : "text-muted-foreground"
    )}>
      {value.baseline ?? 0}→{value.candidate ?? 0}
    </span>
  );
}

export function RunStatus({ run }: { run: WorkbenchRun }) {
  const tone = isActiveRun(run.status)
    ? "blue"
    : run.status === "succeeded"
      ? "green"
      : run.status === "skipped"
        ? "amber"
        : "red";
  const label = isActiveRun(run.status)
    ? "运行中"
    : run.status === "succeeded"
      ? "已完成"
      : run.status === "skipped"
        ? "已跳过"
        : "失败";
  return <Pill tone={tone}>{label}</Pill>;
}

export function Verdict({ value }: { value?: string }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  const tone = value === "accept" ? "green" : value === "reject" ? "red" : "amber";
  const label = value === "accept" ? "指标优化" : value === "reject" ? "指标退化" : "结论不确定";
  return <Pill tone={tone}>{label}</Pill>;
}

export function Cell({ children }: { children: ReactNode }) {
  return <td className="border-b border-line px-4 py-2.5 align-top text-sm">{children}</td>;
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block min-w-0 max-w-full">
      <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted-soft">{hint}</span>}
    </label>
  );
}

export function formatContent(content: WorkbenchTraceMessage["content"]): string {
  if (typeof content === "string") return content;
  if (content == null) return "";
  try {
    return JSON.stringify(content, null, 2);
  } catch {
    return String(content);
  }
}

export function formatArguments(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export const INLINE_MATERIAL_SECTION_RE =
  /(^|\n)([ \t]*(?:材料|素材|输入数据|参考资料)\s*[：:])[ \t]*\n?([\s\S]*)$/i;

export function inlineMaterialText(query?: string): string {
  const match = INLINE_MATERIAL_SECTION_RE.exec(String(query || ""));
  return match?.[3]?.trim() || "";
}

export function hasInlineMaterialSection(query?: string): boolean {
  return INLINE_MATERIAL_SECTION_RE.test(String(query || ""));
}

export function replaceInlineMaterial(query: string, material: string): string {
  const match = INLINE_MATERIAL_SECTION_RE.exec(String(query || ""));
  if (!match || match.index == null) return query;
  const prefix = query.slice(0, match.index);
  return `${prefix}${match[1]}${match[2]}\n${material}`;
}

export function checklistLines(value?: string): string[] {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*/, "").trim())
    .filter(Boolean);
}

export function editableChecklistLines(value?: string): string[] {
  if (!value) return [];
  return String(value)
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*/, ""));
}

export function commonMaterialRoot(materials: WorkbenchMaterial[]): string {
  const paths = materials.map((item) => item.path).filter(Boolean);
  if (!paths.length || !paths[0].includes("/")) return "";
  const first = paths[0].split("/")[0];
  return paths.every((path) => path.startsWith(`${first}/`)) ? first : "";
}
