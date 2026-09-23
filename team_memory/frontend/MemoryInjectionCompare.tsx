import { useState } from "react";
import { FlaskConical, Loader2, Search } from "lucide-react";
import { api, type MemoryReplayBranch, type MemoryTrueReplay } from "@/api/client";
import { Empty, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { DiffView } from "@/components/WorkspaceDiffView";
import type { ScopeName } from "@/views/OpenVikingWorkspaceShell";

type MemoryDebugItem = {
  scope: string;
  space: string;
  title: string;
  path_alias: string;
  l0?: string;
  l1?: string;
  score?: number;
};

type MemoryDebugResult = {
  items: MemoryDebugItem[];
  agent_context: string;
  budget: {
    used_items: number;
    used_chars: number;
    max_items: number;
    max_chars: number;
    truncated: boolean;
  };
};

// Memory experiment panel with two modes:
//  - 注入对比: preview what Agent Context retrieves/injects for a query.
//  - 真回放 A/B: run the Agent twice (stored memory vs draft) and compare
//    turns / tool calls / tokens, same engine as Skill True Replay.
type MemoryReplayResult = MemoryTrueReplay;

export function MemoryInjectionCompare({
  userId,
  scopeName,
  scopeLabel,
  fileName,
  memoryUri,
  originalContent,
  draftContent,
  isDirty,
}: {
  userId: string;
  scopeName: ScopeName;
  scopeLabel: string;
  fileName: string;
  memoryUri: string;
  originalContent: string;
  draftContent: string;
  isDirty: boolean;
}) {
  const [tab, setTab] = useState<"inject" | "replay">("inject");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<MemoryDebugResult | null>(null);
  const [loading, setLoading] = useState(false);

  const [replayChecklist, setReplayChecklist] = useState("");
  const [replaySessionId, setReplaySessionId] = useState("");
  const [replayResult, setReplayResult] = useState<MemoryReplayResult | null>(null);
  const [replaying, setReplaying] = useState(false);

  async function runDebug() {
    if (!query.trim() || !userId) return;
    setLoading(true);
    try {
      setResult(
        await api<MemoryDebugResult>("/api/openviking/memory/debug", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: userId,
            query,
            max_items: 12,
            max_chars: 16000,
          }),
        }),
      );
    } catch (error: any) {
      toastErr("Memory 注入调试失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  async function runReplay() {
    if (!query.trim()) {
      toastErr("请先填写 Agent Query");
      return;
    }
    if (!replayChecklist.trim()) {
      toastErr("请至少填写一条 Checklist");
      return;
    }
    setReplaying(true);
    setReplayResult(null);
    try {
      const replay = await api<MemoryReplayResult>(
        "/api/openviking/memory/true-replay",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            memory_path: memoryUri,
            scope: scopeName,
            before_content: originalContent,
            after_content: draftContent,
            query,
            checklist: replayChecklist,
            ...(replaySessionId.trim()
              ? { source_session_id: replaySessionId.trim() }
              : {}),
          }),
        },
      );
      setReplayResult(replay);
      toastOk("Memory 真回放完成", replay.replay_id || "");
    } catch (error: any) {
      toastErr("Memory 真回放失败", error.message);
    } finally {
      setReplaying(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex rounded-lg bg-muted p-0.5 text-xs font-semibold">
        <button
          type="button"
          className={cn(
            "flex-1 rounded-md px-3 py-1.5",
            tab === "inject" ? "bg-background shadow-sm" : "text-muted-foreground",
          )}
          onClick={() => setTab("inject")}
        >
          注入对比（快速预览）
        </button>
        <button
          type="button"
          className={cn(
            "flex-1 rounded-md px-3 py-1.5",
            tab === "replay" ? "bg-background shadow-sm" : "text-muted-foreground",
          )}
          onClick={() => setTab("replay")}
        >
          真回放 A/B（深度验证）
        </button>
      </div>

      <section className="rounded-lg border border-border bg-surface">
        <div className="border-b border-line px-4 py-2.5 text-sm font-semibold">
          记忆改动差异 · {scopeLabel} / {fileName || "未选择"}
        </div>
        <div className="h-[220px]">
          <DiffView original={originalContent} next={draftContent} />
        </div>
      </section>

      {tab === "inject" ? (
        <section className="rounded-lg border border-border bg-surface">
          <div className="border-b border-line bg-amber-500/5 px-4 py-2 text-[11px] leading-relaxed text-amber-700">
            注入对比读取当前已保存的记忆，展示某 Query 会召回并注入哪些上下文。
            {isDirty && " 草稿尚未保存，预览基于已保存版本；如需验证草稿改动请用「真回放 A/B」。"}
          </div>
          <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
            <label className="min-w-[320px] flex-1 text-xs font-semibold text-muted-foreground">
              Agent Query
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => event.key === "Enter" && void runDebug()}
                className="mt-1"
                placeholder="输入 Agent 当前任务，查看会召回并注入哪些个人 / 团队 Memory"
              />
            </label>
            <Button onClick={() => void runDebug()} disabled={loading || !query.trim()}>
              <Search className="size-4" />
              {loading ? "检索中…" : "模拟注入"}
            </Button>
          </div>
          {result ? (
            <div className="grid gap-4 p-4 lg:grid-cols-[1fr_1.1fr]">
              <div>
                <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                  检索命中
                  <Pill tone="gray">
                    {result.budget.used_items} 条 · {result.budget.used_chars} 字符
                  </Pill>
                </div>
                <div className="max-h-[420px] space-y-2 overflow-auto">
                  {result.items.length ? (
                    result.items.map((item) => (
                      <div
                        key={`${item.scope}-${item.path_alias}`}
                        className="rounded-md border border-border p-3"
                      >
                        <div className="flex items-center gap-2">
                          <Pill tone={item.space === "personal" ? "blue" : "purple"}>
                            {item.space === "personal" ? "个人" : "团队"}
                          </Pill>
                          <strong className="text-xs">{item.title}</strong>
                          {item.score != null && (
                            <span className="ml-auto text-[10px] text-muted-foreground">
                              score {item.score}
                            </span>
                          )}
                        </div>
                        <p className="mt-1.5 whitespace-pre-wrap text-[11px] leading-6 text-muted-foreground">
                          {item.l0 || item.l1 || "无摘要"}
                        </p>
                      </div>
                    ))
                  ) : (
                    <Empty>该 Query 未命中任何 Memory。</Empty>
                  )}
                </div>
              </div>
              <div>
                <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                  实际注入 Agent 的上下文
                  <Pill tone={result.budget.truncated ? "amber" : "green"}>
                    {result.budget.truncated ? "已截断" : "预算内"}
                  </Pill>
                </div>
                <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-3 font-mono text-[11px] leading-6">
                  {result.agent_context || "（无注入内容）"}
                </pre>
              </div>
            </div>
          ) : (
            <div className="p-4">
              <Empty>输入 Agent Query 后，可查看个人与团队 Memory 命中及最终注入文本。</Empty>
            </div>
          )}
        </section>
      ) : (
        <section className="rounded-lg border border-border bg-surface">
          <div className="border-b border-line bg-blue-500/5 px-4 py-2 text-[11px] leading-relaxed text-blue-700">
            真回放 A/B：在同一 Source Session 上下文中，Baseline 注入已保存版本、Candidate 注入当前草稿，
            各跑一次 Agent，对比轮次 / Tool / Tokens 与 Checklist 完成度。
            {!isDirty && " 当前草稿与已保存版本一致，请先在左侧编辑记忆再回放。"}
          </div>
          <div className="space-y-3 border-b border-line px-4 py-3">
            <label className="block text-xs font-semibold text-muted-foreground">
              Agent Query
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="mt-1"
                placeholder="Agent 要完成的任务，例如：为客户 A 公司起草迁移方案"
              />
            </label>
            <label className="block text-xs font-semibold text-muted-foreground">
              完成 Checklist（每行一条）
              <Textarea
                value={replayChecklist}
                onChange={(event) => setReplayChecklist(event.target.value)}
                rows={4}
                className="mono mt-1 text-xs"
                placeholder={"例如：\n引用了正确的迁移流程\n输出包含关键联系人"}
              />
            </label>
            <div className="flex flex-wrap items-end gap-3">
              <label className="min-w-[280px] flex-1 text-xs font-semibold text-muted-foreground">
                指定 Source Session（可选）
                <Input
                  value={replaySessionId}
                  onChange={(event) => setReplaySessionId(event.target.value)}
                  className="mt-1"
                  placeholder="留空则自动选取近期可回放会话"
                />
              </label>
              <Button
                onClick={() => void runReplay()}
                disabled={replaying || !isDirty || !query.trim() || !replayChecklist.trim()}
              >
                <FlaskConical className="size-4" />
                {replaying ? "回放中…" : "运行真回放 A/B"}
              </Button>
            </div>
          </div>
          <div className="p-4">
            {replaying ? (
              <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在并行执行 Baseline / Candidate 分支…
              </div>
            ) : replayResult ? (
              <MemoryReplayResultView result={replayResult} />
            ) : (
              <Empty>填写 Query 与 Checklist 后运行，可对比改动前后 Agent 的真实执行差异。</Empty>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

const MEMORY_METRICS = [
  { key: "interaction_turns", label: "轮次" },
  { key: "tool_call_count", label: "Tool 调用" },
  { key: "total_tokens", label: "Tokens" },
] as const;

function MemoryReplayResultView({ result }: { result: MemoryReplayResult }) {
  const dimensions = result.efficiency?.dimensions || {};
  const verdictTone =
    result.verdict === "accept" ? "green" : result.verdict === "reject" ? "red" : "amber";
  const verdictLabel =
    result.verdict === "accept" ? "改动更优" : result.verdict === "reject" ? "改动退化" : "无显著差异";
  const replayCase = result.cases?.[0];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={verdictTone}>{verdictLabel}</Pill>
        <Pill tone={result.status === "evaluated" ? "green" : "red"}>
          {result.status === "evaluated" ? "已评估" : "失败"}
        </Pill>
        {result.reason && (
          <span className="text-[11px] text-muted-foreground">{result.reason}</span>
        )}
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        {MEMORY_METRICS.map((metric) => {
          const dim = dimensions[metric.key] || {};
          const delta = Number(dim.delta || 0);
          return (
            <div key={metric.key} className="rounded-lg border border-border bg-surface p-3">
              <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">
                {metric.label}
              </div>
              <div className="flex items-baseline gap-1.5 text-base font-bold">
                <span>{Number(dim.baseline || 0).toLocaleString()}</span>
                <span className="text-muted-soft">→</span>
                <span>{Number(dim.candidate || 0).toLocaleString()}</span>
              </div>
              <div
                className={cn(
                  "mt-1 text-[11px] font-semibold",
                  delta > 0 ? "text-emerald-600" : delta < 0 ? "text-rose-600" : "text-muted-foreground",
                )}
              >
                {delta > 0 ? `减少 ${delta.toLocaleString()}` : delta < 0 ? `增加 ${Math.abs(delta).toLocaleString()}` : "持平"}
              </div>
            </div>
          );
        })}
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <MemoryReplayBranchView title="Baseline · 已保存版本" branch={replayCase?.baseline} tone="gray" />
        <MemoryReplayBranchView title="Candidate · 当前草稿" branch={replayCase?.candidate} tone="blue" />
      </div>
    </div>
  );
}

function MemoryReplayBranchView({
  title,
  branch,
  tone,
}: {
  title: string;
  branch?: MemoryReplayBranch;
  tone: "gray" | "blue";
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-background/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface-subtle px-4 py-2.5">
        <span className="text-xs font-semibold">{title}</span>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone={tone}>{branch?.interaction_turns ?? 0} 轮</Pill>
          <Pill tone={tone}>{branch?.tool_call_count ?? 0} tools</Pill>
          <Pill tone={tone}>{Number(branch?.total_tokens || 0).toLocaleString()} tokens</Pill>
          <Pill tone={branch?.ok ? "green" : "red"}>{branch?.ok ? "完成" : "失败"}</Pill>
        </div>
      </div>
      {branch?.error && (
        <pre className="mono whitespace-pre-wrap break-words border-b border-line bg-rose-50 p-3 text-[11px] text-rose-700">
          {branch.error}
        </pre>
      )}
      <pre className="mono max-h-[280px] overflow-auto whitespace-pre-wrap break-words p-3 text-[11px] leading-6">
        {branch?.final_response || "（无输出）"}
      </pre>
    </section>
  );
}
