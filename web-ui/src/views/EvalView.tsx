import { useEffect, useRef, useState } from "react";
import { Panel, Pill, StatCard, type PillTone } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import {
  AlertTriangle,
  CheckCircle2,
  DatabaseZap,
  GitBranch,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Square,
  Upload,
  ArrowRight,
} from "lucide-react";

interface LiftTask {
  name: string;
  query: string;
  requirements: {
    default_skills: string[];
    extra_skills_dir: string;
    material_dir: string;
  };
  expected_result: {
    content_reqs: string;
    trajectory_reqs: string;
  };
}

interface LiftSuite {
  name: string;
  category: string;
  warmup_tasks: LiftTask[];
  holdout_tasks: LiftTask[];
}

interface LiftValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  warmup_count: number;
  holdout_count: number;
  task_count: number;
}

interface LiftManifest {
  id: string;
  state: "draft" | "approved" | "published";
  origin?: string;
  source_skill: string;
  suite_slug: string;
  created_at?: string;
  updated_at?: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  published_at?: string | null;
  published_paths?: Record<string, string>;
  metrics?: {
    warmup_count?: number;
    holdout_count?: number;
    dimension_overlap_pct?: number;
  };
  validation: LiftValidation;
}

interface LiftIntegration {
  repository_url: string;
  compatibility_revision: string;
  installed_revision: string;
  revision_compatible: boolean;
  schema: string;
  root: string;
  root_configured: boolean;
  checkout_ready: boolean;
  python: string;
  python_version: string;
  python_ready: boolean;
  docker_ready: boolean;
  datasets_root: string;
  supported_runtimes: string[];
}

interface SourceSkill {
  name: string;
  has_evaluation: boolean;
  has_benchmark: boolean;
  question_count: number;
}

interface LiftRunner {
  state: "idle" | "running" | "stopping" | "stopped" | "done" | "error";
  current?: {
    id?: string;
    suite?: string;
    runtime?: string;
    result_dir?: string;
    exit_code?: number | null;
  } | null;
}

interface LiftStatus {
  integration: LiftIntegration;
  source_skills: SourceSkill[];
  drafts: LiftManifest[];
  runner: LiftRunner;
}

interface DraftDetail {
  manifest: LiftManifest;
  suite: LiftSuite;
}

function stateTone(state: LiftManifest["state"]): PillTone {
  return state === "published" ? "green" : state === "approved" ? "blue" : "amber";
}

function stateLabel(state: LiftManifest["state"]): string {
  return state === "published" ? "已发布" : state === "approved" ? "审核通过" : "待审核";
}

export default function EvalView({
  active,
  user,
  onOpenMining,
}: {
  active: boolean;
  user?: UserProfile | null;
  onOpenMining?: () => void;
}) {
  const [status, setStatus] = useState<LiftStatus | null>(null);
  const [detail, setDetail] = useState<DraftDetail | null>(null);
  const [busy, setBusy] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [runtime, setRuntime] = useState("hermes");
  const [repeat, setRepeat] = useState(1);
  const [form, setForm] = useState({ skill_name: "", suite_name: "", category: "", warmup_ratio: 0.67 });
  const logRef = useRef<HTMLDivElement | null>(null);
  const lastSeq = useRef(0);
  const streamId = useRef("");

  async function refreshStatus(showSpinner = true) {
    if (showSpinner) setRefreshing(true);
    try {
      const next = await api<LiftStatus>("/api/mining/lift/status");
      setStatus(next);
      setForm((prev) => {
        if (prev.skill_name) return prev;
        const first = next.source_skills.find((skill) => skill.has_benchmark);
        return first ? { ...prev, skill_name: first.name, suite_name: first.name, category: first.name } : prev;
      });
    } catch (e: any) {
      toastErr("加载 LIFT 状态失败", e.message);
    } finally {
      if (showSpinner) setRefreshing(false);
    }
  }

  useEffect(() => {
    if (!active) return;
    refreshStatus();
    const es = new EventSource("/api/mining/events");
    es.onmessage = (event) => {
      let data: any;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (typeof data.stream_id === "string" && data.stream_id !== streamId.current) {
        streamId.current = data.stream_id;
        lastSeq.current = 0;
      }
      if (typeof data.seq === "number") {
        if (data.seq <= lastSeq.current) return;
        lastSeq.current = data.seq;
      }
      if (data.type === "lift_log" && typeof data.msg === "string") {
        setLogs((prev) => [...prev.slice(-600), data.msg]);
      }
      if (data.type === "lift_status" || data.type === "lift_done") {
        setStatus((prev) => prev ? { ...prev, runner: { state: data.state, current: data.run } } : prev);
        if (data.type === "lift_done") refreshStatus(false);
      }
    };
    return () => es.close();
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  if (!active) return null;

  const integration = status?.integration;
  const drafts = status?.drafts ?? [];
  const runner = status?.runner;
  const running = runner?.state === "running" || runner?.state === "stopping";
  const publishable = detail?.manifest.state === "approved" && !dirty;
  const runnable = !!detail && detail.manifest.state === "published"
    && !dirty
    && !!integration?.checkout_ready
    && !!integration.python_ready
    && !!integration.docker_ready;
  const canApprove = !!detail && !busy && (
    dirty || (detail.manifest.state === "draft" && detail.manifest.validation.valid)
  );
  const runBlocker = !detail
    ? "先从审核队列选择一个 Suite"
    : dirty
      ? "存在未保存修改，请先保存并重新校验"
      : detail.manifest.state === "draft"
        ? "请先完成人工审核"
        : detail.manifest.state === "approved"
          ? "请先发布到 LIFT 工作区"
          : !integration?.checkout_ready
            ? "LIFT 工作区尚未就绪"
            : !integration.python_ready
              ? `LIFT 需要 Python 3.12+，当前为 ${integration.python_version || "未知版本"}`
              : !integration.docker_ready
                ? "未检测到 Docker，无法启动完整对照评测"
                : "已发布，可运行完整 LIFT 对照";

  async function createDraft() {
    if (!form.skill_name) return;
    setBusy("create");
    try {
      const result = await api<DraftDetail & { ok: boolean }>("/api/mining/lift/drafts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      setDetail({ manifest: result.manifest, suite: result.suite });
      setDirty(false);
      await refreshStatus(false);
      toastOk("LIFT 草稿已生成", `${result.manifest.validation.task_count} 道任务，等待人工审核`);
    } catch (e: any) {
      toastErr("生成 LIFT 草稿失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function openDraft(id: string) {
    setBusy(`open:${id}`);
    try {
      setDetail(await api<DraftDetail>(`/api/mining/lift/drafts/${id}`));
      setDirty(false);
    } catch (e: any) {
      toastErr("读取草稿失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function saveDraft(showToast = true): Promise<DraftDetail | null> {
    if (!detail) return null;
    setBusy("save");
    try {
      const result = await api<DraftDetail & { ok: boolean }>(
        `/api/mining/lift/drafts/${detail.manifest.id}/save`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ suite: detail.suite }),
        }
      );
      const next = { manifest: result.manifest, suite: result.suite };
      setDetail(next);
      setDirty(false);
      await refreshStatus(false);
      if (showToast) toastOk("草稿已保存", "任何修改都会重新进入待审核状态");
      return next;
    } catch (e: any) {
      toastErr("保存草稿失败", e.message);
      return null;
    } finally {
      setBusy("");
    }
  }

  async function approveDraft() {
    const saved = await saveDraft(false);
    if (!saved) return;
    if (!saved.manifest.validation.valid) {
      toastErr("草稿仍有结构错误", saved.manifest.validation.errors[0] || "请修正后重新提交审核");
      return;
    }
    setBusy("approve");
    try {
      const result = await api<DraftDetail & { ok: boolean }>(
        `/api/mining/lift/drafts/${saved.manifest.id}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reviewer: user?.display_name || user?.id || "human-reviewer" }),
        }
      );
      setDetail({ manifest: result.manifest, suite: result.suite });
      setDirty(false);
      await refreshStatus(false);
      toastOk("人工审核已通过", "现在可以发布到外部 LIFT 工作区");
    } catch (e: any) {
      toastErr("审核未通过", e.message);
    } finally {
      setBusy("");
    }
  }

  async function publishDraft() {
    if (!detail) return;
    setBusy("publish");
    try {
      const result = await api<DraftDetail & { ok: boolean }>(
        `/api/mining/lift/drafts/${detail.manifest.id}/publish`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
      );
      setDetail({ manifest: result.manifest, suite: result.suite });
      setDirty(false);
      await refreshStatus(false);
      toastOk("已发布到 LIFT", result.manifest.published_paths?.suite_json || "");
    } catch (e: any) {
      toastErr("发布到 LIFT 失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function startLift() {
    if (!detail) return;
    setBusy("run");
    setLogs([]);
    try {
      await api("/api/mining/lift/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          suite: `${detail.manifest.suite_slug}.json`,
          runtime,
          repeat,
          max_parallel_suites: 1,
        }),
      });
      await refreshStatus(false);
      toastOk("LIFT 评测已启动", `${runtime} · ${detail.manifest.suite_slug}.json`);
    } catch (e: any) {
      toastErr("启动 LIFT 失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function stopLift() {
    try {
      await api("/api/mining/lift/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      toastOk("已发送停止信号", "LIFT 正在清理运行资源");
    } catch (e: any) {
      toastErr("停止 LIFT 失败", e.message);
    }
  }

  function updateSuite(key: "name" | "category", value: string) {
    setDirty(true);
    setDetail((prev) => prev ? {
      ...prev,
      manifest: { ...prev.manifest, state: "draft" },
      suite: { ...prev.suite, [key]: value },
    } : prev);
  }

  function updateTask(
    split: "warmup_tasks" | "holdout_tasks",
    index: number,
    updater: (task: LiftTask) => LiftTask
  ) {
    setDirty(true);
    setDetail((prev) => {
      if (!prev) return prev;
      const tasks = [...prev.suite[split]];
      tasks[index] = updater(tasks[index]);
      return { ...prev, manifest: { ...prev.manifest, state: "draft" }, suite: { ...prev.suite, [split]: tasks } };
    });
  }

  function moveTask(split: "warmup_tasks" | "holdout_tasks", index: number) {
    const target = split === "warmup_tasks" ? "holdout_tasks" : "warmup_tasks";
    if (!detail || detail.suite[split].length <= 1) return;
    setDirty(true);
    setDetail((prev) => {
      if (!prev) return prev;
      const sourceTasks = [...prev.suite[split]];
      const [task] = sourceTasks.splice(index, 1);
      const targetTasks = [...prev.suite[target], task];
      return { ...prev, manifest: { ...prev.manifest, state: "draft" }, suite: { ...prev.suite, [split]: sourceTasks, [target]: targetTasks } };
    });
  }

  return (
    <div>
      <div className="border-b border-line bg-surface px-7 py-5">
        <h1 className="flex items-center gap-2.5 text-[22px] font-bold tracking-tight">
          LIFT 评测中心
          <Pill tone="purple">Suite v1</Pill>
          {running && <Pill tone="blue">运行中</Pill>}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          将 SkillMiner 题库转换为 LIFT Suite，经人工审核后发布并运行 warmup → holdout baseline/evolved 对照评测。
        </p>
      </div>

      <div className="px-7 py-6">
        <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard label="LIFT 工作区" value={integration?.checkout_ready ? "已就绪" : "未配置"} />
          <StatCard label="待审核草稿" value={String(drafts.filter((item) => item.state === "draft").length)} />
          <StatCard label="已发布 Suite" value={String(drafts.filter((item) => item.state === "published").length)} />
          <StatCard label="运行状态" value={runnerStateLabel(runner?.state)} />
        </div>

        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
          <Panel
            title="集成状态"
            extra={<Pill tone={integration?.checkout_ready ? "green" : "amber"}>{integration?.checkout_ready ? "工作区已连接" : "待安装"}</Pill>}
          >
            <div className="grid gap-3 p-4 md:grid-cols-3">
              <StatusItem label="外部 Checkout" ok={!!integration?.checkout_ready} value={integration?.root || "加载中…"} />
              <StatusItem
                label="LIFT Python"
                ok={!!integration?.python_ready}
                value={integration?.python ? `${integration.python} · ${integration.python_version || "版本未知"}` : "—"}
              />
              <StatusItem label="Docker" ok={!!integration?.docker_ready} value={integration?.docker_ready ? "已发现" : "未发现"} />
            </div>
            {!integration?.checkout_ready && (
              <div className="mx-4 mb-4 rounded-lg border border-amber-300/50 bg-amber-50/50 p-3 text-xs leading-relaxed text-muted-foreground">
                <div className="mb-1 font-bold text-foreground">准备外部 LIFT 工作区</div>
                <code className="mono">bash scripts/setup_lift.sh</code>
                <span>；完整运行还需要按 LIFT 文档准备 Docker、Langfuse、凭据与 runtime 镜像。</span>
              </div>
            )}
            {integration?.checkout_ready && (!integration.python_ready || !integration.docker_ready) && (
              <div className="mx-4 mb-4 rounded-lg border border-amber-300/50 bg-amber-50/50 p-3 text-xs leading-relaxed text-muted-foreground">
                <span className="font-bold text-foreground">审核与发布可继续；完整运行尚缺：</span>
                {!integration.python_ready && <span> Python 3.12+</span>}
                {!integration.python_ready && !integration.docker_ready && <span>、</span>}
                {!integration.docker_ready && <span> Docker</span>}。
              </div>
            )}
            {integration?.checkout_ready && integration.installed_revision && !integration.revision_compatible && (
              <div className="mx-4 mb-4 rounded-lg border border-amber-300/50 bg-amber-50/50 p-3 text-xs leading-relaxed text-muted-foreground">
                当前 checkout revision <span className="mono">{integration.installed_revision.slice(0, 12)}</span> 与已审计版本不同；请在运行前重新验证上游契约，或使用 <code className="mono">scripts/setup_lift.sh</code> 切回兼容版本。
              </div>
            )}
          </Panel>

          <Panel title="生成待审核草稿">
            <div className="space-y-3 p-4">
              <Field label="来源 Skill">
                <select
                  value={form.skill_name}
                  onChange={(e) => {
                    const name = e.target.value;
                    setForm((prev) => ({ ...prev, skill_name: name, suite_name: name, category: name }));
                  }}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                >
                  <option value="">选择已生成 Benchmark 的 Skill</option>
                  {(status?.source_skills ?? []).map((skill) => (
                    <option key={skill.name} value={skill.name} disabled={!skill.has_benchmark}>
                      {skill.name} · {skill.has_benchmark ? `${skill.question_count} 题` : "缺少 benchmark"}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Suite 名称">
                <Input value={form.suite_name} onChange={(e) => setForm((prev) => ({ ...prev, suite_name: e.target.value }))} />
              </Field>
              <Field label="场景分类">
                <Input value={form.category} onChange={(e) => setForm((prev) => ({ ...prev, category: e.target.value }))} />
              </Field>
              <Field label={`Warmup 比例 · ${Math.round(form.warmup_ratio * 100)}%`}>
                <input
                  type="range"
                  min="0.5"
                  max="0.8"
                  step="0.01"
                  value={form.warmup_ratio}
                  onChange={(e) => setForm((prev) => ({ ...prev, warmup_ratio: Number(e.target.value) }))}
                  className="w-full accent-[var(--accent)]"
                />
              </Field>
              <Button className="w-full" disabled={!form.skill_name || busy === "create"} onClick={createDraft}>
                <DatabaseZap className="size-4" /> {busy === "create" ? "生成中…" : "生成 LIFT 草稿"}
              </Button>
            </div>
          </Panel>
        </div>

        <Panel title="审核队列" count={`${drafts.length} 个版本`} extra={<Button size="sm" variant="outline" onClick={() => refreshStatus()} disabled={refreshing}><RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />{refreshing ? "刷新中…" : "刷新"}</Button>}>
          {drafts.length === 0 ? (
            <div className="flex flex-col items-center gap-3 p-8 text-center text-sm text-muted-soft">
              <span>还没有待审核 Suite。先为已编译技能生成 Benchmark，系统会自动创建草稿。</span>
              <Button size="sm" variant="outline" onClick={onOpenMining}>去生成 Benchmark <ArrowRight className="size-3.5" /></Button>
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="w-full border-collapse text-[13px]">
                <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                  <th className="px-4 py-2.5">Suite / 来源</th><th className="px-4 py-2.5">任务划分</th><th className="px-4 py-2.5">校验</th><th className="px-4 py-2.5">状态</th><th className="px-4 py-2.5 text-right">操作</th>
                </tr></thead>
                <tbody>{drafts.map((item) => (
                  <tr key={item.id} className={cn("border-b border-line last:border-none", detail?.manifest.id === item.id && "bg-accent-soft/50")}>
                    <td className="px-4 py-3"><div className="font-semibold">{item.suite_slug}</div><div className="mono mt-0.5 text-[11px] text-muted-foreground">{item.source_skill}</div></td>
                    <td className="px-4 py-3 text-muted-foreground">{item.validation.warmup_count} warmup / {item.validation.holdout_count} holdout</td>
                    <td className="px-4 py-3">{item.validation.valid ? <Pill tone="green">通过</Pill> : <Pill tone="red">{item.validation.errors.length} 错误</Pill>}</td>
                    <td className="px-4 py-3"><Pill tone={stateTone(item.state)}>{stateLabel(item.state)}</Pill></td>
                    <td className="px-4 py-3 text-right"><Button size="sm" variant="outline" onClick={() => openDraft(item.id)} disabled={busy === `open:${item.id}`}>{item.state === "draft" ? "审核" : "查看"}</Button></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </Panel>

        {detail && (
          <Panel
            title={`人工审核 · ${detail.manifest.id}`}
            extra={<div className="flex items-center gap-2"><Pill tone={dirty ? "amber" : stateTone(detail.manifest.state)}>{dirty ? "未保存" : stateLabel(detail.manifest.state)}</Pill><Pill tone={dirty ? "gray" : detail.manifest.validation.valid ? "green" : "red"}>{dirty ? "待重新校验" : detail.manifest.validation.valid ? "结构有效" : "需修正"}</Pill></div>}
          >
            <div className="space-y-5 p-4">
              <ReviewProgress manifest={detail.manifest} dirty={dirty} />

              <div className="grid gap-3 md:grid-cols-2">
                <Field label="Suite name"><Input value={detail.suite.name} onChange={(e) => updateSuite("name", e.target.value)} /></Field>
                <Field label="Category"><Input value={detail.suite.category} onChange={(e) => updateSuite("category", e.target.value)} /></Field>
              </div>

              {dirty ? (
                <div className="rounded-lg border border-blue-200 bg-blue-50/50 p-3 text-xs leading-relaxed text-muted-foreground">
                  当前修改尚未保存；保存后会重新执行结构校验，并将已审核版本重新置为待审核。
                </div>
              ) : (detail.manifest.validation.errors.length > 0 || detail.manifest.validation.warnings.length > 0) && (
                <div className="rounded-lg border border-amber-300/50 bg-amber-50/40 p-3 text-xs leading-relaxed">
                  {detail.manifest.validation.errors.map((message) => <div key={message} className="text-danger">错误：{message}</div>)}
                  {detail.manifest.validation.warnings.map((message) => <div key={message} className="text-amber-700">提示：{message}</div>)}
                </div>
              )}

              <TaskGroup
                title="Warmup tasks · 练习与进化"
                tone="blue"
                tasks={detail.suite.warmup_tasks}
                onUpdate={(index, updater) => updateTask("warmup_tasks", index, updater)}
                onMove={(index) => moveTask("warmup_tasks", index)}
                moveLabel="移至 Holdout"
              />
              <TaskGroup
                title="Holdout tasks · Baseline / Evolved 期末对照"
                tone="purple"
                tasks={detail.suite.holdout_tasks}
                onUpdate={(index, updater) => updateTask("holdout_tasks", index, updater)}
                onMove={(index) => moveTask("holdout_tasks", index)}
                moveLabel="移至 Warmup"
              />

              <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-4">
                <Button variant="outline" onClick={() => saveDraft()} disabled={!!busy || !dirty}><Save className="size-4" />保存并重新校验</Button>
                <Button variant="outline" onClick={approveDraft} disabled={!canApprove}><ShieldCheck className="size-4" />保存并审核通过</Button>
                <Button onClick={publishDraft} disabled={!!busy || dirty || !publishable || !integration?.checkout_ready}><Upload className="size-4" />发布到 LIFT</Button>
              </div>
            </div>
          </Panel>
        )}

        <Panel title="运行 LIFT" extra={<Pill tone={running ? "blue" : runner?.state === "error" ? "red" : "gray"}>{runnerStateLabel(runner?.state)}</Pill>}>
          <div className="grid gap-4 p-4 lg:grid-cols-[260px_100px_1fr_auto] lg:items-end">
            <Field label="Runtime">
              <select value={runtime} onChange={(e) => setRuntime(e.target.value)} disabled={running} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm">
                {(integration?.supported_runtimes ?? ["hermes"]).map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </Field>
            <Field label="Repeat"><Input type="number" min={1} max={5} value={repeat} disabled={running} onChange={(e) => setRepeat(Number(e.target.value))} /></Field>
            <div className="rounded-lg border border-border bg-background px-3 py-2 text-xs text-muted-foreground">
              {detail && <><span className="font-semibold text-foreground">{detail.manifest.suite_slug}.json</span><br /></>}{runBlocker}
            </div>
            <div className="flex gap-2">
              <Button title={!runnable ? runBlocker : undefined} onClick={startLift} disabled={!runnable || running || busy === "run"}><Play className="size-4" />启动</Button>
              <Button variant="outline" onClick={stopLift} disabled={!running}><Square className="size-4" />停止</Button>
            </div>
          </div>
          <div ref={logRef} className="mono max-h-80 min-h-28 overflow-auto whitespace-pre-wrap break-words bg-[#0f172a] p-4 text-[11.5px] leading-relaxed text-[#cbd5e1]">
            {logs.length ? logs.map((line, index) => <div key={index}>{line}</div>) : <span className="text-muted-soft">LIFT 运行日志将在这里显示。完整结果由 LIFT 写入其 results/lift-runid-*/ 目录。</span>}
          </div>
        </Panel>

        <div className="flex items-start gap-3 rounded-lg border border-border bg-surface p-4 text-xs leading-relaxed text-muted-foreground">
          <GitBranch className="mt-0.5 size-4 shrink-0 text-accent" />
          <div><span className="font-semibold text-foreground">兼容边界：</span>当前适配基于 LIFT Suite v1 与审计 revision <span className="mono">{integration?.compatibility_revision?.slice(0, 12) || "—"}</span>。上游代码保持外部 checkout，不进入 SkillGene 安装包。</div>
        </div>
      </div>
    </div>
  );
}

function StatusItem({ label, ok, value }: { label: string; ok: boolean; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold">
        {ok ? <CheckCircle2 className="size-4 text-success" /> : <AlertTriangle className="size-4 text-amber-500" />}
        {label}
      </div>
      <div className="mono break-all text-[11px] text-muted-foreground">{value}</div>
    </div>
  );
}

function runnerStateLabel(state?: LiftRunner["state"]) {
  return {
    idle: "空闲",
    running: "运行中",
    stopping: "停止中",
    stopped: "已停止",
    done: "已完成",
    error: "失败",
  }[state || "idle"];
}

function ReviewProgress({ manifest, dirty }: { manifest: LiftManifest; dirty: boolean }) {
  const steps = [
    { label: "保存并校验", done: !dirty && manifest.validation.valid },
    { label: "人工审核", done: !dirty && (manifest.state === "approved" || manifest.state === "published") },
    { label: "发布到 LIFT", done: !dirty && manifest.state === "published" },
  ];
  const activeIndex = steps.findIndex((step) => !step.done);
  return (
    <div className="grid gap-2 sm:grid-cols-3" aria-label="Suite 审核进度">
      {steps.map((step, index) => (
        <div
          key={step.label}
          className={cn(
            "flex items-center gap-2 rounded-lg border px-3 py-2.5 text-xs font-semibold",
            step.done
              ? "border-success/30 bg-emerald-50/50 text-success"
              : index === activeIndex
                ? "border-accent/40 bg-accent-soft text-accent"
                : "border-border bg-background text-muted-foreground"
          )}
        >
          <span className={cn("grid size-5 shrink-0 place-items-center rounded-full text-[10px]", step.done ? "bg-success text-white" : "bg-muted text-muted-foreground")}>
            {step.done ? <CheckCircle2 className="size-3.5" /> : index + 1}
          </span>
          {step.label}
        </div>
      ))}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</Label>{children}</div>;
}

function TaskGroup({
  title,
  tone,
  tasks,
  onUpdate,
  onMove,
  moveLabel,
}: {
  title: string;
  tone: PillTone;
  tasks: LiftTask[];
  onUpdate: (index: number, updater: (task: LiftTask) => LiftTask) => void;
  onMove: (index: number) => void;
  moveLabel: string;
}) {
  return (
    <div>
      <div className="mb-3 flex items-center gap-2"><Pill tone={tone}>{tasks.length}</Pill><h3 className="text-sm font-bold">{title}</h3></div>
      <div className="space-y-3">
        {tasks.map((task, index) => (
          <div key={index} className="rounded-lg border border-border bg-background p-3.5">
            <div className="mb-3 flex items-center gap-2">
              <Input
                value={task.name}
                className="max-w-36 font-semibold"
                onChange={(e) => onUpdate(index, (current) => ({ ...current, name: e.target.value }))}
              />
              <span className="text-xs text-muted-foreground">Task {index + 1}</span>
              <Button className="ml-auto" size="sm" variant="outline" disabled={tasks.length <= 1} onClick={() => onMove(index)}>{moveLabel}</Button>
            </div>
            <div className="grid gap-3 xl:grid-cols-3">
              <Field label="Query · 首次只给 Agent 的自然语言任务">
                <Textarea rows={6} value={task.query} onChange={(e) => onUpdate(index, (current) => ({ ...current, query: e.target.value }))} />
              </Field>
              <Field label="内容要求 · LLM Judge 验收清单">
                <Textarea
                  rows={6}
                  value={task.expected_result.content_reqs}
                  onChange={(e) => onUpdate(index, (current) => ({ ...current, expected_result: { ...current.expected_result, content_reqs: e.target.value } }))}
                />
              </Field>
              <Field label="轨迹要求 · 工具与执行路径约束">
                <Textarea
                  rows={6}
                  value={task.expected_result.trajectory_reqs}
                  onChange={(e) => onUpdate(index, (current) => ({ ...current, expected_result: { ...current.expected_result, trajectory_reqs: e.target.value } }))}
                />
              </Field>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
