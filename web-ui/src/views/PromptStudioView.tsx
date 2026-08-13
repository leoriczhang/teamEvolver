import { useCallback, useEffect, useRef, useState } from "react";
import { Empty, Panel, Pill, type PillTone } from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  api,
  type PipelineGraph,
  type PipelineNode,
  type PromptDetail,
  type PromptStudioSession,
  type PromptSummary,
  type PromptTestResult,
  type UserProfile,
} from "@/api/client";
import { cn } from "@/lib/utils";
import { toastErr, toastOk } from "@/lib/toast";

const KIND_TONE: Record<PipelineNode["kind"], PillTone> = {
  io: "gray",
  llm: "blue",
  logic: "purple",
  gate: "amber",
};

const KIND_LABEL: Record<PipelineNode["kind"], string> = {
  io: "输入/输出",
  llm: "LLM 阶段",
  logic: "逻辑",
  gate: "校验门禁",
};

export default function PromptStudioView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const isAdmin = user?.role === "admin";
  const [graph, setGraph] = useState<PipelineGraph | null>(null);
  const [prompts, setPrompts] = useState<PromptSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [sessions, setSessions] = useState<PromptStudioSession[]>([]);
  const [testSession, setTestSession] = useState<string>("");
  const [testResult, setTestResult] = useState<PromptTestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [showDefault, setShowDefault] = useState(false);
  const loaded = useRef(false);

  const loadDetail = useCallback(async (stageId: string) => {
    try {
      const d = await api<PromptDetail>(`/api/prompt-studio/prompts/${stageId}`);
      setDetail(d);
      setDraft(d.effective_prompt);
      setTestResult(null);
    } catch (e: any) {
      toastErr("加载 prompt 详情失败", e.message);
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [g, p, s] = await Promise.all([
        api<PipelineGraph>("/api/prompt-studio/pipeline"),
        api<{ prompts: PromptSummary[] }>("/api/prompt-studio/prompts"),
        api<{ sessions: PromptStudioSession[] }>("/api/prompt-studio/sessions?limit=50"),
      ]);
      setGraph(g);
      setPrompts(p.prompts || []);
      setSessions(s.sessions || []);
      if (!selectedId && p.prompts?.length) {
        const first = p.prompts[0].id;
        setSelectedId(first);
        await loadDetail(first);
      }
      if (!testSession && s.sessions?.length) {
        setTestSession(s.sessions[0].session_id);
      }
    } catch (e: any) {
      toastErr("加载 Prompt Studio 失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [selectedId, testSession, loadDetail]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  async function selectPrompt(stageId: string) {
    setSelectedId(stageId);
    setShowDefault(false);
    await loadDetail(stageId);
  }

  async function save() {
    if (!isAdmin || !detail) return;
    setSaving(true);
    try {
      const d = await api<PromptDetail>(`/api/prompt-studio/prompts/${detail.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: draft }),
      });
      setDetail(d);
      setDraft(d.effective_prompt);
      toastOk("Prompt 已保存并生效", "下次进化流程立即使用新 prompt");
      // Refresh list so the override badge updates.
      const p = await api<{ prompts: PromptSummary[] }>("/api/prompt-studio/prompts");
      setPrompts(p.prompts || []);
      const g = await api<PipelineGraph>("/api/prompt-studio/pipeline");
      setGraph(g);
    } catch (e: any) {
      toastErr("保存 prompt 失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    if (!isAdmin || !detail) return;
    setSaving(true);
    try {
      const d = await api<PromptDetail>(`/api/prompt-studio/prompts/${detail.id}/reset`, {
        method: "POST",
      });
      setDetail(d);
      setDraft(d.effective_prompt);
      toastOk("已恢复默认 prompt", detail.label);
      const p = await api<{ prompts: PromptSummary[] }>("/api/prompt-studio/prompts");
      setPrompts(p.prompts || []);
      const g = await api<PipelineGraph>("/api/prompt-studio/pipeline");
      setGraph(g);
    } catch (e: any) {
      toastErr("恢复默认失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    if (!detail) return;
    if (!testSession) {
      toastErr("无法测试", "请先选择一个会话作为测试输入");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api<PromptTestResult>(`/api/prompt-studio/prompts/${detail.id}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: testSession, prompt: draft }),
      });
      setTestResult(result);
      toastOk("测试完成", "已返回真实的 system / user / 输出");
    } catch (e: any) {
      toastErr("prompt 测试失败", e.message);
    } finally {
      setTesting(false);
    }
  }

  const dirty = !!detail && draft !== detail.effective_prompt;

  return (
    <div className="mx-auto max-w-[1280px] px-[22px] py-[22px]">
      {/* ---- Pipeline chain visualization ---- */}
      <Panel
        title="进化链路"
        extra={
          <Button variant="ghost" size="sm" onClick={refresh} disabled={loading}>
            刷新
          </Button>
        }
      >
        <div className="p-4">
          <PipelineChain graph={graph} selectedPrompt={selectedId} onPick={selectPrompt} />
          <div className="mt-3 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
            {(["llm", "logic", "gate", "io"] as const).map((k) => (
              <span key={k} className="flex items-center gap-1.5">
                <Pill tone={KIND_TONE[k]}>{KIND_LABEL[k]}</Pill>
              </span>
            ))}
            <span className="ml-auto">点击蓝色「LLM 阶段」节点可直接编辑其 prompt。</span>
          </div>
        </div>
      </Panel>

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* ---- Prompt list ---- */}
        <Panel title="可编辑 Prompt" count={`${prompts.length} 个`}>
          <div className="flex flex-col p-2">
            {prompts.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => selectPrompt(p.id)}
                className={cn(
                  "flex items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left text-sm transition-colors",
                  selectedId === p.id ? "bg-sidebar-accent font-semibold" : "hover:bg-muted"
                )}
              >
                <span className="min-w-0 flex-1 truncate">{p.label}</span>
                {p.overridden && <Pill tone="amber">已改</Pill>}
              </button>
            ))}
            {!prompts.length && <Empty>暂无 prompt。</Empty>}
          </div>
        </Panel>

        {/* ---- Editor + test ---- */}
        <div>
          {!detail ? (
            <Panel title="Prompt 编辑器">
              <Empty>选择左侧一个 prompt 开始查看与编辑。</Empty>
            </Panel>
          ) : (
            <>
              <Panel
                title={
                  <span className="flex items-center gap-2">
                    {detail.label}
                    {detail.overridden ? <Pill tone="amber">自定义</Pill> : <Pill tone="gray">默认</Pill>}
                    {dirty && <Pill tone="blue">未保存</Pill>}
                  </span>
                }
                extra={
                  <div className="flex items-center gap-2">
                    <Button variant="ghost" size="sm" onClick={() => setShowDefault((v) => !v)}>
                      {showDefault ? "隐藏默认" : "对照默认"}
                    </Button>
                    {detail.overridden && (
                      <Button variant="outline" size="sm" onClick={reset} disabled={!isAdmin || saving}>
                        恢复默认
                      </Button>
                    )}
                    <Button size="sm" onClick={save} disabled={!isAdmin || saving || !dirty}>
                      {saving ? "保存中…" : "保存并生效"}
                    </Button>
                  </div>
                }
              >
                <div className="space-y-3 p-4">
                  <div className="text-xs text-muted-foreground">{detail.description}</div>
                  <div className="flex flex-wrap gap-2 text-[11px]">
                    <Pill tone="gray">temperature {detail.temperature}</Pill>
                    <Pill tone="gray">
                      {detail.module}.{detail.symbol}
                    </Pill>
                    {detail.injects_shared_blocks && <Pill tone="purple">支持共享块 __…__</Pill>}
                  </div>
                  {!!detail.variables?.length && (
                    <div className="rounded-lg border border-border bg-background/60 p-3 text-xs">
                      <div className="mb-1 font-semibold text-muted-foreground">该阶段的输入变量（user 消息由这些构成）</div>
                      <ul className="list-inside list-disc space-y-0.5 text-muted-foreground">
                        {detail.variables.map((v) => (
                          <li key={v} className="mono">{v}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {!isAdmin && (
                    <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
                      当前账号不是管理员，只能查看 prompt，无法保存或测试。
                    </div>
                  )}

                  <div className={cn("grid gap-3", showDefault && "lg:grid-cols-2")}>
                    <div>
                      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">当前 System Prompt（可编辑）</div>
                      <textarea
                        value={draft}
                        disabled={!isAdmin}
                        onChange={(e) => setDraft(e.target.value)}
                        spellCheck={false}
                        className="mono h-[420px] w-full resize-y rounded-lg border border-border bg-background p-3 text-xs leading-relaxed outline-none focus:border-sidebar-primary"
                      />
                      <div className="mt-1 text-[11px] text-muted-soft">{draft.length} 字符</div>
                    </div>
                    {showDefault && (
                      <div>
                        <div className="mb-1.5 text-xs font-semibold text-muted-foreground">模块默认 System Prompt（只读）</div>
                        <textarea
                          value={detail.default_prompt}
                          readOnly
                          spellCheck={false}
                          className="mono h-[420px] w-full resize-y rounded-lg border border-border bg-surface-subtle p-3 text-xs leading-relaxed text-muted-foreground outline-none"
                        />
                        <div className="mt-1 text-[11px] text-muted-soft">
                          {detail.default_prompt.length} 字符
                          <button
                            type="button"
                            className="ml-2 underline"
                            onClick={() => setDraft(detail.default_prompt)}
                            disabled={!isAdmin}
                          >
                            用默认覆盖编辑区
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </Panel>

              {/* ---- Test panel ---- */}
              <Panel title="测试运行（真实输入/输出，非黑盒）">
                <div className="space-y-3 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="text-xs font-semibold text-muted-foreground">测试会话</label>
                    <select
                      value={testSession}
                      onChange={(e) => setTestSession(e.target.value)}
                      className="h-8 min-w-[280px] max-w-full rounded-lg border border-border bg-background px-2 text-xs outline-none"
                    >
                      <option value="">（选择一个会话作为输入）</option>
                      {sessions.map((s) => (
                        <option key={s.session_id} value={s.session_id}>
                          {(s.title || s.session_id).slice(0, 60)} · {s.num_turns ?? "?"}轮
                        </option>
                      ))}
                    </select>
                    <Button size="sm" onClick={runTest} disabled={!isAdmin || testing || !testSession}>
                      {testing ? "运行中…" : "用当前编辑内容测试"}
                    </Button>
                    <span className="ml-auto text-[11px] text-muted-foreground">
                      使用编辑区的 prompt（不必先保存）+ 所选会话，调用进化模型返回真实结果。
                    </span>
                  </div>

                  {testResult ? (
                    <div className="grid gap-3 lg:grid-cols-3">
                      <IoBlock title="① System Prompt（实际下发）" body={testResult.system_prompt} />
                      <IoBlock title="② User 消息（由会话构造）" body={testResult.user_message} />
                      <IoBlock title="③ 模型输出" body={testResult.output} highlight />
                    </div>
                  ) : (
                    <Empty>选择会话后点击「测试」，即可看到该阶段实际的 system prompt、user 输入和模型输出。</Empty>
                  )}
                </div>
              </Panel>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function PipelineChain({
  graph,
  selectedPrompt,
  onPick,
}: {
  graph: PipelineGraph | null;
  selectedPrompt: string;
  onPick: (stageId: string) => void;
}) {
  if (!graph?.nodes?.length) {
    return <Empty>链路加载中…</Empty>;
  }
  // Render as an ordered horizontal flow. The catalog is authored in flow order,
  // so a simple wrapped row with arrows communicates the chain clearly.
  return (
    <div className="flex flex-wrap items-stretch gap-2">
      {graph.nodes.map((node, i) => {
        const isLlm = node.kind === "llm";
        const clickable = isLlm && !!node.prompt_id;
        const selected = clickable && node.prompt_id === selectedPrompt;
        return (
          <div key={node.id} className="flex items-stretch gap-2">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && node.prompt_id && onPick(node.prompt_id)}
              title={node.description}
              className={cn(
                "flex w-[150px] flex-col gap-1 rounded-lg border p-2.5 text-left transition-colors",
                selected
                  ? "border-sidebar-primary bg-accent-soft"
                  : clickable
                    ? "border-blue-300 bg-blue-50/40 hover:border-sidebar-primary"
                    : "border-border bg-surface-subtle",
                !clickable && "cursor-default"
              )}
            >
              <span className="flex items-center justify-between gap-1">
                <Pill tone={KIND_TONE[node.kind]}>{KIND_LABEL[node.kind]}</Pill>
                {node.overridden && <Pill tone="amber">改</Pill>}
              </span>
              <span className="text-[12.5px] font-semibold leading-tight">{node.label}</span>
              <span className="line-clamp-3 text-[10.5px] leading-snug text-muted-foreground">
                {node.description}
              </span>
            </button>
            {i < graph.nodes.length - 1 && (
              <span className="flex items-center text-muted-soft">→</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function IoBlock({ title, body, highlight }: { title: string; body: string; highlight?: boolean }) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">{title}</div>
      <pre
        className={cn(
          "mono h-[340px] overflow-auto whitespace-pre-wrap break-words rounded-lg border p-3 text-[11px] leading-relaxed",
          highlight ? "border-sidebar-primary bg-accent-soft" : "border-border bg-background"
        )}
      >
        {body || "（空）"}
      </pre>
      <div className="mt-1 text-[11px] text-muted-soft">{(body || "").length} 字符</div>
    </div>
  );
}
