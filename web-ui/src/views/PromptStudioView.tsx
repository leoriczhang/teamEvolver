import { useCallback, useEffect, useRef, useState } from "react";
import { Empty, Panel, Pill, type PillTone } from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  api,
  type EvolveProcessSettings,
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

interface StageRuntimeDraft {
  model: string;
  temperature: number;
  max_tokens: number;
}

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
  const [runtimeDraft, setRuntimeDraft] = useState<StageRuntimeDraft>({
    model: "",
    temperature: 0.3,
    max_tokens: 8192,
  });
  const [processSettings, setProcessSettings] = useState<EvolveProcessSettings | null>(null);
  const [savedProcessSettings, setSavedProcessSettings] = useState("");
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
      setRuntimeDraft({
        model: d.model || "",
        temperature: Number(d.temperature ?? d.default_temperature ?? 0.3),
        max_tokens: Number(d.max_tokens || d.default_max_tokens || 8192),
      });
      setTestResult(null);
    } catch (e: any) {
      toastErr("加载 prompt 详情失败", e.message);
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [g, p, s, process] = await Promise.all([
        api<PipelineGraph>("/api/prompt-studio/pipeline"),
        api<{ prompts: PromptSummary[] }>("/api/prompt-studio/prompts"),
        api<{ sessions: PromptStudioSession[] }>("/api/prompt-studio/sessions?limit=50"),
        api<EvolveProcessSettings>("/api/evolve-settings"),
      ]);
      setGraph(g);
      setPrompts(p.prompts || []);
      setProcessSettings(process);
      setSavedProcessSettings(JSON.stringify(process));
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
        body: JSON.stringify({ prompt: draft, settings: runtimeDraft }),
      });
      if (processSettings) {
        const savedProcess = await api<EvolveProcessSettings>("/api/evolve-settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ evolve: processSettings.evolve, validation: processSettings.validation }),
        });
        setProcessSettings(savedProcess);
        setSavedProcessSettings(JSON.stringify(savedProcess));
      }
      setDetail(d);
      setDraft(d.effective_prompt);
      setRuntimeDraft({
        model: d.model || "",
        temperature: Number(d.temperature ?? d.default_temperature ?? 0.3),
        max_tokens: Number(d.max_tokens || d.default_max_tokens || 8192),
      });
      toastOk("阶段配置已保存并生效", "Prompt、模型和过程参数已同步更新");
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
      setRuntimeDraft({
        model: d.model || "",
        temperature: Number(d.temperature ?? d.default_temperature ?? 0.3),
        max_tokens: Number(d.max_tokens || d.default_max_tokens || 8192),
      });
      toastOk("已恢复阶段默认配置", detail.label);
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

  const dirty = !!detail && (
    draft !== detail.effective_prompt
    || (!!processSettings && JSON.stringify(processSettings) !== savedProcessSettings)
    || runtimeDraft.model !== (detail.model || "")
    || runtimeDraft.temperature !== Number(detail.temperature ?? 0.3)
    || runtimeDraft.max_tokens !== Number(detail.max_tokens || 8192)
  );

  return (
    <div className="mx-auto max-w-[1280px] px-[22px] py-[22px]">
      {/* ---- Pipeline chain visualization ---- */}
      <Panel
        title="Skills 自进化链路"
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
                {(p.overridden || p.settings_overridden) && <Pill tone="amber">已改</Pill>}
              </button>
            ))}
            {!prompts.length && <Empty>暂无 prompt。</Empty>}
          </div>
          <div className="border-t border-line p-3">
            <div className="mb-2 text-xs font-bold text-muted-foreground">测试数据源</div>
            <select
              value={testSession}
              onChange={(event) => setTestSession(event.target.value)}
              className="h-9 w-full rounded-lg border border-border bg-background px-2 text-xs outline-none"
            >
              <option value="">选择真实 Session</option>
              {sessions.map((session) => (
                <option key={session.session_id} value={session.session_id}>
                  {(session.title || session.session_id).slice(0, 32)}
                </option>
              ))}
            </select>
            {testSession && (
              <div className="mt-2 rounded-md bg-muted p-2 text-[10px] leading-4 text-muted-foreground">
                当前阶段测试会使用该 Session 构造真实 User Message。
              </div>
            )}
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
                    {detail.overridden || detail.settings_overridden ? <Pill tone="amber">自定义</Pill> : <Pill tone="gray">默认</Pill>}
                    {dirty && <Pill tone="blue">未保存</Pill>}
                  </span>
                }
                extra={
                  <div className="flex items-center gap-2">
                    <Button variant="ghost" size="sm" onClick={() => setShowDefault((v) => !v)}>
                      {showDefault ? "隐藏默认" : "对照默认"}
                    </Button>
                    {(detail.overridden || detail.settings_overridden) && (
                      <Button variant="outline" size="sm" onClick={reset} disabled={!isAdmin || saving}>
                        全部恢复默认
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
                    <Pill tone="gray">max tokens {detail.max_tokens}</Pill>
                    <Pill tone={detail.model ? "blue" : "gray"}>
                      {detail.model || "继承全局模型"}
                    </Pill>
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

                  <div className="grid gap-3 rounded-lg border border-border bg-background/60 p-3 md:grid-cols-3">
                    <div>
                      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">阶段模型</div>
                      <input
                        value={runtimeDraft.model}
                        disabled={!isAdmin}
                        placeholder="留空继承全局模型"
                        onChange={(event) => setRuntimeDraft({
                          ...runtimeDraft,
                          model: event.target.value,
                        })}
                        className="mono h-9 w-full rounded-md border border-border bg-background px-3 text-xs outline-none focus:border-sidebar-primary"
                      />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">Temperature</div>
                      <input
                        type="number"
                        min="0"
                        max="2"
                        step="0.1"
                        value={runtimeDraft.temperature}
                        disabled={!isAdmin}
                        onChange={(event) => setRuntimeDraft({
                          ...runtimeDraft,
                          temperature: Number(event.target.value),
                        })}
                        className="mono h-9 w-full rounded-md border border-border bg-background px-3 text-xs outline-none focus:border-sidebar-primary"
                      />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">最大输出 Token</div>
                      <input
                        type="number"
                        min="1"
                        value={runtimeDraft.max_tokens}
                        disabled={!isAdmin}
                        onChange={(event) => setRuntimeDraft({
                          ...runtimeDraft,
                          max_tokens: Number(event.target.value),
                        })}
                        className="mono h-9 w-full rounded-md border border-border bg-background px-3 text-xs outline-none focus:border-sidebar-primary"
                      />
                    </div>
                    <div className="text-[11px] text-muted-foreground md:col-span-3">
                      当前默认：{detail.default_temperature} temperature · {detail.default_max_tokens} max tokens。阶段模型留空时继承「进化模型」。
                    </div>
                  </div>
                  {processSettings && (
                    <StageProcessSettings
                      stageId={detail.id}
                      settings={processSettings}
                      disabled={!isAdmin}
                      onChange={setProcessSettings}
                    />
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
                    <Pill tone={testSession ? "blue" : "gray"}>
                      {testSession ? "已从左侧选择测试数据" : "请在左侧选择测试数据"}
                    </Pill>
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
  // Derive layered columns from the real edges (longest-path levelling) so
  // parallel branches — e.g. Evolve 与 Create 同时从 Group 分叉 — render as
  // stacked cards in one column instead of a misleading single line. A node's
  // level is 1 + the max level of its predecessors.
  const predecessors = new Map<string, string[]>();
  for (const edge of graph.edges) {
    predecessors.set(edge.to, [...(predecessors.get(edge.to) || []), edge.from]);
  }
  const levelOf = new Map<string, number>();
  const computeLevel = (id: string, seen: Set<string>): number => {
    const cached = levelOf.get(id);
    if (cached !== undefined) return cached;
    // Guard against cycles (the graph is a DAG, but stay defensive).
    if (seen.has(id)) return 0;
    seen.add(id);
    const preds = predecessors.get(id) || [];
    const level = preds.length
      ? Math.max(...preds.map((p) => computeLevel(p, seen))) + 1
      : 0;
    levelOf.set(id, level);
    return level;
  };
  for (const node of graph.nodes) computeLevel(node.id, new Set());

  const columns: PipelineNode[][] = [];
  for (const node of graph.nodes) {
    const level = levelOf.get(node.id) ?? 0;
    (columns[level] ||= []).push(node);
  }
  const filledColumns = columns.filter(Boolean);

  // A node reachable by more than one distinct upstream branch (e.g. Merge,
  // which only runs when a same-name conflict occurs) is conditional, not a
  // guaranteed step. Flag it so the chain doesn't imply it always fires.
  const isConditional = (id: string) => (predecessors.get(id) || []).length > 1;

  return (
    <div className="flex items-stretch gap-1.5 overflow-x-auto pb-1">
      {filledColumns.map((column, colIndex) => (
        <div key={colIndex} className="flex items-stretch gap-1.5">
          <div className="flex flex-col justify-center gap-2">
            {column.map((node) => {
              const isLlm = node.kind === "llm";
              const clickable = isLlm && !!node.prompt_id;
              const selected = clickable && node.prompt_id === selectedPrompt;
              const conditional = isConditional(node.id);
              return (
                <button
                  key={node.id}
                  type="button"
                  disabled={!clickable}
                  onClick={() => clickable && node.prompt_id && onPick(node.prompt_id)}
                  title={
                    conditional
                      ? `${node.description || node.label}（条件触发：仅在满足前置条件时执行）`
                      : node.description
                  }
                  className={cn(
                    "flex w-[150px] flex-col gap-1 rounded-lg border p-2.5 text-left transition-colors",
                    selected
                      ? "border-sidebar-primary bg-accent-soft"
                      : clickable
                        ? "border-blue-300 bg-blue-50/40 hover:border-sidebar-primary"
                        : "border-border bg-surface-subtle",
                    conditional && "border-dashed",
                    !clickable && "cursor-default"
                  )}
                >
                  <span className="flex flex-wrap items-center justify-between gap-1">
                    <Pill tone={KIND_TONE[node.kind]}>{KIND_LABEL[node.kind]}</Pill>
                    <span className="flex items-center gap-1">
                      {conditional && <Pill tone="gray">条件</Pill>}
                      {node.overridden && <Pill tone="amber">改</Pill>}
                    </span>
                  </span>
                  <span className="text-[12.5px] font-semibold leading-tight">{node.label}</span>
                  <span className="line-clamp-3 text-[10.5px] leading-snug text-muted-foreground">
                    {node.description}
                  </span>
                </button>
              );
            })}
            {column.length > 1 && (
              <span className="text-center text-[10px] font-semibold text-muted-soft">
                并行分支
              </span>
            )}
          </div>
          {colIndex < filledColumns.length - 1 && (
            <span className="flex items-center text-muted-soft">→</span>
          )}
        </div>
      ))}
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

type StageParam = {
  group: "evolve" | "validation";
  key: string;
  label: string;
  kind: "boolean" | "number" | "select" | "text";
  options?: Array<{ value: string; label: string }>;
};

const STAGE_PROCESS_PARAMS: Record<string, StageParam[]> = {
  session_filter: [
    { group: "evolve", key: "interval_seconds", label: "进化轮询周期（秒）", kind: "number" },
  ],
  summarize: [
    { group: "evolve", key: "evidence_enabled", label: "启用跨周期 Evidence", kind: "boolean" },
    { group: "evolve", key: "evidence_recent_limit", label: "近期 Evidence 窗口", kind: "number" },
    { group: "evolve", key: "evidence_historical_limit", label: "历史 Evidence 窗口", kind: "number" },
  ],
  judge: [
    { group: "evolve", key: "use_session_judge", label: "启用 Session Judge", kind: "boolean" },
  ],
  evolve_skill: [
    { group: "evolve", key: "evidence_max_entries", label: "Evidence 最大条数", kind: "number" },
    { group: "evolve", key: "evidence_replay_cases_per_window", label: "每窗口回放案例", kind: "number" },
    { group: "evolve", key: "evidence_change_debt_threshold", label: "变更债务阈值", kind: "number" },
    { group: "evolve", key: "bundle_static_checks_enabled", label: "Bundle 静态检查", kind: "boolean" },
  ],
  create_skill: [
    { group: "evolve", key: "candidate_coalesce_enabled", label: "候选变更合并", kind: "boolean" },
    { group: "evolve", key: "bundle_allow_delete", label: "允许删除 Bundle 文件", kind: "boolean" },
    { group: "evolve", key: "bundle_max_prompt_bytes", label: "Prompt 总预算（bytes）", kind: "number" },
    { group: "evolve", key: "bundle_max_file_bytes", label: "单文件上限（bytes）", kind: "number" },
    { group: "evolve", key: "bundle_text_extensions", label: "可编辑扩展名", kind: "text" },
  ],
  merge: [
    { group: "evolve", key: "candidate_coalesce_enabled", label: "启用冲突合并", kind: "boolean" },
  ],
  dataset_synthesis: [
    { group: "evolve", key: "dataset_synthesis_enabled", label: "启用同步生成", kind: "boolean" },
    { group: "evolve", key: "dataset_test_cases", label: "每次测试集数量", kind: "number" },
    { group: "evolve", key: "dataset_min_requirements", label: "最少 Checklist", kind: "number" },
    { group: "evolve", key: "dataset_max_requirements", label: "最多 Checklist", kind: "number" },
    { group: "evolve", key: "dataset_disclosure_batch_size", label: "每轮披露条数", kind: "number" },
  ],
  replay_checklist: [
    { group: "validation", key: "enabled", label: "启用后台验证", kind: "boolean" },
    { group: "validation", key: "mode", label: "验证模式", kind: "select", options: [{ value: "true_replay", label: "True Replay" }, { value: "replay", label: "Replay" }] },
    { group: "evolve", key: "publish_mode", label: "发布模式", kind: "select", options: [{ value: "validated", label: "验证后发布" }, { value: "direct", label: "直接发布" }] },
    { group: "evolve", key: "validation_max_rejections", label: "验证拒绝上限", kind: "number" },
    { group: "evolve", key: "human_review_enabled", label: "启用人工复核", kind: "boolean" },
    { group: "evolve", key: "human_review_timeout_seconds", label: "人工复核超时（秒）", kind: "number" },
    { group: "validation", key: "max_concurrency", label: "最大并发", kind: "number" },
    { group: "validation", key: "required_results", label: "所需验证结果", kind: "number" },
    { group: "validation", key: "required_approvals", label: "所需通过数", kind: "number" },
    { group: "validation", key: "idle_after_seconds", label: "空闲后启动（秒）", kind: "number" },
    { group: "validation", key: "poll_interval_seconds", label: "验证轮询周期（秒）", kind: "number" },
    { group: "validation", key: "max_jobs_per_day", label: "每日任务上限", kind: "number" },
  ],
};

function StageProcessSettings({
  stageId,
  settings,
  disabled,
  onChange,
}: {
  stageId: string;
  settings: EvolveProcessSettings;
  disabled: boolean;
  onChange: (settings: EvolveProcessSettings) => void;
}) {
  const params = STAGE_PROCESS_PARAMS[stageId] || [];
  function update(param: StageParam, value: boolean | number | string) {
    const group = settings[param.group] as unknown as Record<string, unknown>;
    onChange({ ...settings, [param.group]: { ...group, [param.key]: value } });
  }
  return (
    <div className="rounded-lg border border-border bg-background/60 p-3">
      <div className="mb-1 text-xs font-bold">该阶段过程参数</div>
      <div className="mb-3 text-[11px] text-muted-foreground">
        这些参数与当前 Prompt 属于同一阶段，点击“保存并生效”时一起持久化。
      </div>
      {!params.length ? (
        <div className="text-xs text-muted-foreground">该阶段没有独立过程参数，只使用上方模型采样参数。</div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {params.map((param) => {
            const value = (settings[param.group] as unknown as Record<string, unknown>)[param.key];
            if (param.kind === "boolean") {
              return <label key={`${param.group}.${param.key}`} className="flex items-center justify-between rounded-md border bg-background px-3 py-2 text-xs font-semibold"><span>{param.label}</span><input type="checkbox" disabled={disabled} checked={Boolean(value)} onChange={(event) => update(param, event.target.checked)} /></label>;
            }
            if (param.kind === "select") {
              return <label key={`${param.group}.${param.key}`} className="text-xs font-semibold">{param.label}<select disabled={disabled} value={String(value ?? "")} onChange={(event) => update(param, event.target.value)} className="mt-1 block h-9 w-full rounded-md border bg-background px-2">{param.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
            }
            if (param.kind === "text") {
              return <label key={`${param.group}.${param.key}`} className="text-xs font-semibold">{param.label}<input type="text" disabled={disabled} value={Array.isArray(value) ? value.join(", ") : String(value ?? "")} onChange={(event) => update(param, event.target.value)} className="mt-1 block h-9 w-full rounded-md border bg-background px-3" /></label>;
            }
            return <label key={`${param.group}.${param.key}`} className="text-xs font-semibold">{param.label}<input type="number" min="0" disabled={disabled} value={Number(value ?? 0)} onChange={(event) => update(param, Number(event.target.value))} className="mt-1 block h-9 w-full rounded-md border bg-background px-3" /></label>;
          })}
        </div>
      )}
    </div>
  );
}
