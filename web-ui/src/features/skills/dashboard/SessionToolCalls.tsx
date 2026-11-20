import { PaginationControls,usePagedItems } from "@/components/common";
import { type SessionToolCall,type SessionToolResult } from "@/features/sessions/api/client";
import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import { useMemo,useState } from "react";

function parseArguments(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  return JSON.stringify(value, null, 2) ?? "";
}

function argumentSummary(value: unknown): string {
  if (value === null || value === undefined || value === "") return "无参数";
  if (typeof value === "object" && !Array.isArray(value)) {
    const fields = value as Record<string, unknown>;
    for (const key of ["description", "skill", "file_path", "path", "command"]) {
      if (typeof fields[key] === "string" && fields[key]) return compact(fields[key]);
    }
    if (Array.isArray(fields.todos)) return `${fields.todos.length} 项任务`;
    return Object.keys(fields).join(" · ") || "无参数";
  }
  return compact(valueText(value));
}

function compact(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > 120 ? `${line.slice(0, 120)}…` : line;
}

function groupResults(calls: SessionToolCall[], results: SessionToolResult[]) {
  const indexes = new Map<string, number[]>();
  calls.forEach((call, index) => {
    if (call.id) indexes.set(call.id, [...(indexes.get(call.id) || []), index]);
  });
  const matched = calls.map(() => [] as SessionToolResult[]);
  const unmatched: SessionToolResult[] = [];
  results.forEach((result) => {
    const targets = result.tool_call_id ? indexes.get(result.tool_call_id) : undefined;
    if (targets?.length === 1) matched[targets[0]].push(result);
    else unmatched.push(result);
  });
  return { matched, unmatched };
}

function ToolResult({ result }: { result: SessionToolResult }) {
  return (
    <div className={cn("min-w-0 rounded-md border border-border p-2", result.has_error && "border-destructive/30 bg-destructive/5")}>
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        {result.tool_name && <span className="font-mono">{result.tool_name}</span>}
        {result.has_error && <span className="font-semibold text-destructive">调用出错</span>}
      </div>
      <pre className={cn("max-h-[320px] overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed [overflow-wrap:anywhere]", result.has_error && "text-destructive")}>
        {valueText(parseArguments(result.content)) || "（空结果）"}
      </pre>
    </div>
  );
}

function ToolCallItem({ call, index, results }: { call: SessionToolCall; index: number; results: SessionToolResult[] }) {
  const [expanded, setExpanded] = useState(false);
  const name = call.function?.name || call.name || "unknown";
  const raw = call.function?.arguments ?? call.input;
  const value = parseArguments(raw);
  const fields = value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value as Record<string, unknown>)
    : null;

  return (
    <div className="min-w-0 overflow-hidden rounded-md border border-border bg-background">
      <button
        type="button"
        aria-expanded={expanded}
        aria-label={`${expanded ? "收起" : "展开"}第 ${index} 次工具调用 ${name}`}
        className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left hover:bg-muted/50"
        onClick={() => setExpanded((previous) => !previous)}
      >
        <span className="w-5 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">{index}</span>
        <span className="shrink-0 font-mono text-xs font-semibold">{name}</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{argumentSummary(value)}</span>
        <span className={cn("shrink-0 text-[10px]", results.some((result) => result.has_error) ? "text-destructive" : "text-muted-foreground")}>
          {results.some((result) => result.has_error) ? "调用出错" : results.length ? `结果 ${results.length}` : "暂无结果"}
        </span>
        <ChevronDown aria-hidden="true" className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${expanded ? "rotate-180" : ""}`} />
      </button>
      {expanded && (
        <div className="space-y-3 border-t border-border px-3 py-3">
          <div className="text-[11px] font-semibold text-muted-foreground">调用参数</div>
          <div className="max-h-[320px] space-y-3 overflow-auto">
            {fields?.length ? fields.map(([key, fieldValue]) => (
              <div key={key} className="min-w-0">
                <div className="mb-1 font-mono text-[11px] font-semibold text-muted-foreground">{key}</div>
                <pre className="whitespace-pre-wrap break-words rounded bg-muted/40 p-2 font-mono text-[11px] leading-relaxed [overflow-wrap:anywhere]">
                  {valueText(fieldValue)}
                </pre>
              </div>
            )) : (
              <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed [overflow-wrap:anywhere]">
                {fields ? "无参数" : valueText(value) || "无参数"}
              </pre>
            )}
          </div>
          <details>
            <summary className="cursor-pointer text-[11px] text-muted-foreground">原始参数</summary>
            <pre className="mt-2 max-h-[240px] overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] [overflow-wrap:anywhere]">
              {valueText(raw) || "无参数"}
            </pre>
          </details>
          <div className="space-y-2 border-t border-border pt-3">
            <div className="text-[11px] font-semibold text-muted-foreground">调用结果（{results.length}）</div>
            {results.length ? results.map((result, resultIndex) => (
              <ToolResult key={resultIndex} result={result} />
            )) : (
              <p className="text-[11px] text-muted-foreground">该调用暂无归档结果。</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function SessionToolCalls({ calls, results = [] }: { calls: SessionToolCall[]; results?: SessionToolResult[] }) {
  const pager = usePagedItems(calls, 10);
  const grouped = useMemo(() => groupResults(calls, results), [calls, results]);
  return (
    <section aria-label="工具调用" className="mt-2 min-w-0 rounded-lg border border-border bg-background/60 p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2 text-[11px]">
        <span className="font-semibold text-muted-foreground">工具调用（{calls.length}） · 结果（{results.length}）</span>
        <span className="text-muted-foreground">点击查看参数与结果</span>
      </div>
      <div className="space-y-1.5">
        {pager.items.map((call, index) => (
          <ToolCallItem key={`${call.id || "call"}-${pager.start + index}`} call={call} index={pager.start + index + 1} results={grouped.matched[pager.start + index]} />
        ))}
      </div>
      <PaginationControls {...pager} onPageChange={pager.setPage} />
      {!!grouped.unmatched.length && (
        <details className="mt-2 rounded-md border border-border p-2.5">
          <summary className="cursor-pointer text-[11px] font-semibold text-muted-foreground">
            未匹配的结果（{grouped.unmatched.length}）
          </summary>
          <p className="mt-2 text-[11px] text-muted-foreground">缺少对应调用或唯一调用 ID，原始结果保留在这里。</p>
          <div className="mt-2 max-h-[400px] space-y-2 overflow-auto">
            {grouped.unmatched.map((result, index) => <ToolResult key={index} result={result} />)}
          </div>
        </details>
      )}
    </section>
  );
}
