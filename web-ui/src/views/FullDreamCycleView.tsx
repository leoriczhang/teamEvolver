import { useCallback, useEffect, useState } from "react";
import {
  ClipboardList,
  GitCompare,
  History,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
} from "lucide-react";

import {
  api,
  type EvolveProcessSettings,
  type MemoryChangeList,
  type MemoryChangeRecord,
  type MemoryReplayBranch,
  type MemoryTrueReplay,
  type MemoryTrueReplayList,
  type UserProfile,
} from "@/api/client";
import {
  Empty,
  Panel,
  Pill,
  StatCard,
  type PillTone,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toastErr, toastOk } from "@/lib/toast";

type MemorySettings = EvolveProcessSettings["memory_maintenance"];
type DreamCycleJob = MemorySettings["jobs"][number];

type DreamCycleStatus = {
  engine?: string;
  full_capabilities?: boolean;
  enabled?: boolean;
  configured?: boolean;
  running?: boolean;
  daemon_running?: boolean;
  active_window?: boolean;
  total_cycles?: number;
  rounds_today?: number;
  last_run_date?: string;
  last_error?: string;
  report_dir?: string;
  last_results?: Array<Record<string, any>>;
  history?: Array<Record<string, any>>;
};

function normalizeSettings(
  settings: EvolveProcessSettings,
): EvolveProcessSettings {
  const memory = (
    settings.memory_maintenance || {}
  ) as MemorySettings;
  return {
    ...settings,
    memory_maintenance: {
      ...memory,
      enabled: Boolean(memory.enabled),
      auto_start: Boolean(memory.auto_start),
      model: memory.model || "",
      base_url: memory.base_url || "",
      engine:
        memory.engine || "teamEvolver-native-dreamcycle",
      full_capabilities: memory.full_capabilities !== false,
      llm_max_tokens: Number(memory.llm_max_tokens || 4096),
      temperature: Number(memory.temperature ?? 0.3),
      customer_id: memory.customer_id || "",
      scheduler: {
        active_start_hour: Number(
          memory.scheduler?.active_start_hour ?? 0,
        ),
        active_end_hour: Number(
          memory.scheduler?.active_end_hour ?? 6,
        ),
        rounds_per_window: Number(
          memory.scheduler?.rounds_per_window || 3,
        ),
        round_interval_minutes: Number(
          memory.scheduler?.round_interval_minutes || 90,
        ),
        max_turns_per_job: Number(
          memory.scheduler?.max_turns_per_job || 25,
        ),
        max_consecutive_errors: Number(
          memory.scheduler?.max_consecutive_errors || 3,
        ),
        retry_delay_seconds: Number(
          memory.scheduler?.retry_delay_seconds || 300,
        ),
      },
      jobs: Array.isArray(memory.jobs) ? memory.jobs : [],
    },
  };
}

export default function FullDreamCycleView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [settings, setSettings] =
    useState<EvolveProcessSettings | null>(null);
  const [status, setStatus] =
    useState<DreamCycleStatus | null>(null);
  const [dryRun, setDryRun] =
    useState<{ jobs?: DreamCycleJob[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [memoryChanges, setMemoryChanges] =
    useState<MemoryChangeRecord[]>([]);
  const [replayChange, setReplayChange] =
    useState<MemoryChangeRecord | null>(null);
  const [replayHistory, setReplayHistory] =
    useState<MemoryTrueReplay[]>([]);
  const [replayQuery, setReplayQuery] = useState("");
  const [replayChecklist, setReplayChecklist] = useState("");
  const [replaySourceSession, setReplaySourceSession] = useState("");
  const [replayMaxInteractions, setReplayMaxInteractions] = useState(4);
  const [replayRunning, setReplayRunning] = useState(false);
  const [replayLoading, setReplayLoading] = useState(false);
  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [config, state, changes] = await Promise.all([
        api<EvolveProcessSettings>("/api/evolve-settings"),
        api<DreamCycleStatus>("/trigger-dreamcycle/status"),
        isAdmin
          ? api<MemoryChangeList>(
              "/trigger-dreamcycle/memory-changes?limit=100",
            )
          : Promise.resolve<MemoryChangeList>({
              schema_version: "",
              count: 0,
              items: [],
            }),
      ]);
      setSettings(normalizeSettings(config));
      setStatus(state);
      setMemoryChanges(changes.items || []);
    } catch (error: any) {
      toastErr("加载 DreamCycle 失败", error.message);
    } finally {
      setLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    if (active) void refresh();
  }, [active, refresh]);

  useEffect(() => {
    if (!active || !status?.running) return;
    const timer = window.setInterval(() => {
      api<DreamCycleStatus>("/trigger-dreamcycle/status")
        .then(setStatus)
        .catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [active, status?.running]);

  if (!settings) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        {loading ? "正在加载完整 DreamCycle…" : "DreamCycle 配置不可用"}
      </div>
    );
  }

  const memory = settings.memory_maintenance;
  const update = (values: Partial<MemorySettings>) => {
    setSettings({
      ...settings,
      memory_maintenance: { ...memory, ...values },
    });
  };
  const updateScheduler = (
    values: Partial<MemorySettings["scheduler"]>,
  ) => {
    update({
      scheduler: { ...memory.scheduler, ...values },
    });
  };
  const updateJob = (
    jobId: string,
    values: Partial<DreamCycleJob>,
  ) => {
    update({
      jobs: memory.jobs.map((job) =>
        job.id === jobId ? { ...job, ...values } : job
      ),
    });
  };

  async function save() {
    if (!isAdmin) return;
    setSaving(true);
    try {
      const next = await api<EvolveProcessSettings>(
        "/api/evolve-settings",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            memory_maintenance: memory,
          }),
        },
      );
      setSettings(normalizeSettings(next));
      toastOk(
        "完整 DreamCycle 配置已保存",
        "Scheduler、ReAct 和 Job 配置已重新加载",
      );
      await refresh();
    } catch (error: any) {
      toastErr("保存 DreamCycle 失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function trigger() {
    setRunning(true);
    try {
      const next = await api<
        DreamCycleStatus & { status?: string }
      >("/trigger-dreamcycle", { method: "POST" });
      setStatus(next);
      toastOk("DreamCycle 已触发", next.status || "started");
    } catch (error: any) {
      toastErr("触发 DreamCycle 失败", error.message);
    } finally {
      setRunning(false);
    }
  }

  async function inspectPlan() {
    try {
      const result = await api<{ jobs: DreamCycleJob[] }>(
        "/trigger-dreamcycle/dry-run",
      );
      setDryRun(result);
      toastOk("执行计划已生成", `${result.jobs?.length || 0} 个 Job`);
    } catch (error: any) {
      toastErr("生成执行计划失败", error.message);
    }
  }

  async function openMemoryReplay(change: MemoryChangeRecord) {
    setReplayChange(change);
    setReplayQuery("");
    setReplayChecklist("");
    setReplaySourceSession("");
    setReplayMaxInteractions(4);
    setReplayHistory(change.latest_replay ? [change.latest_replay] : []);
    setReplayLoading(true);
    try {
      const history = await api<MemoryTrueReplayList>(
        `/trigger-dreamcycle/memory-changes/${encodeURIComponent(change.change_id)}/true-replays?limit=20`,
      );
      setReplayHistory(history.items || []);
    } catch (error: any) {
      toastErr("加载 Memory Replay 历史失败", error.message);
    } finally {
      setReplayLoading(false);
    }
  }

  async function runMemoryReplay() {
    if (!replayChange) return;
    const checklist = replayChecklist
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    if (!replayQuery.trim() || !checklist.length) {
      toastErr("Replay 参数不完整", "Query 和 Checklist 不能为空");
      return;
    }
    setReplayRunning(true);
    try {
      const result = await api<MemoryTrueReplay>(
        `/trigger-dreamcycle/memory-changes/${encodeURIComponent(replayChange.change_id)}/true-replay`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: replayQuery.trim(),
            checklist,
            source_session_id: replaySourceSession.trim(),
            max_interactions: replayMaxInteractions,
            timeout_seconds: 600,
          }),
        },
      );
      setReplayHistory((current) => [
        result,
        ...current.filter((item) => item.replay_id !== result.replay_id),
      ]);
      setMemoryChanges((current) =>
        current.map((item) =>
          item.change_id === replayChange.change_id
            ? { ...item, latest_replay: result }
            : item
        )
      );
      setReplayChange((current) =>
        current ? { ...current, latest_replay: result } : current
      );
      toastOk(
        "Memory True Replay 已完成",
        result.verdict || result.status || "evaluated",
      );
    } catch (error: any) {
      toastErr("Memory True Replay 失败", error.message);
    } finally {
      setReplayRunning(false);
    }
  }

  const selectedReplay =
    replayHistory[0] || replayChange?.latest_replay || null;

  return (
    <div className="mx-auto max-w-[1320px] space-y-5 px-[22px] py-[22px]">
      <div className="content-toolbar">
        <div>
          <div className="flex flex-wrap items-center gap-2 text-sm font-bold">
            团队 Memory 自进化（完整 DreamCycle）
            <Pill tone="green">
              {memory.engine || "teamEvolver-native-dreamcycle"}
            </Pill>
            <Pill tone={memory.full_capabilities ? "blue" : "red"}>
              {memory.full_capabilities ? "完整能力" : "兼容模式"}
            </Pill>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            5 个维护 Job · ReAct 工具调用 · OpenViking
            读写/归档 · 策略审计 · 报告 · 夜间多轮调度
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={loading}
          >
            <RefreshCw className="size-4" />
            刷新
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={inspectPlan}
            disabled={!isAdmin}
          >
            <ClipboardList className="size-4" />
            Dry Run
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={trigger}
            disabled={!isAdmin || running || status?.running}
          >
            <Play className="size-4" />
            {running || status?.running ? "运行中…" : "立即运行一轮"}
          </Button>
          <Button
            size="sm"
            onClick={save}
            disabled={!isAdmin || saving}
          >
            <Save className="size-4" />
            保存全部
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard
          label="运行状态"
          value={status?.running ? "运行中" : "空闲"}
        />
        <StatCard
          label="今日轮次"
          value={status?.rounds_today ?? 0}
        />
        <StatCard
          label="累计周期"
          value={status?.total_cycles ?? 0}
        />
        <StatCard
          label="启用 Jobs"
          value={`${memory.jobs.filter((job) => job.enabled).length}/${memory.jobs.length}`}
        />
        <StatCard
          label="活跃窗口"
          value={`${memory.scheduler.active_start_hour}:00-${memory.scheduler.active_end_hour}:00`}
        />
      </div>

      {!memory.jobs.length && (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-xs text-amber-900">
          当前后端尚未返回完整 DreamCycle Job Catalog。请重启
          teamEvolver 52010 服务后刷新。
        </div>
      )}

      <Panel
        title="运行、模型与调度"
        extra={
          <Pill
            tone={
              status?.running
                ? "amber"
                : status?.configured
                  ? "green"
                  : "gray"
            }
          >
            {status?.running
              ? "执行中"
              : status?.configured
                ? "已配置"
                : "待配置"}
          </Pill>
        }
      >
        <div className="grid gap-4 p-4 md:grid-cols-2 lg:grid-cols-4">
          <ToggleField
            label="启用 DreamCycle"
            checked={memory.enabled}
            disabled={!isAdmin}
            onChange={(enabled) => update({ enabled })}
          />
          <ToggleField
            label="启用夜间自动调度"
            checked={memory.auto_start}
            disabled={!isAdmin}
            onChange={(auto_start) => update({ auto_start })}
          />
          <Field label="独立模型（留空继承全局）">
            <Input
              disabled={!isAdmin}
              value={memory.model}
              onChange={(event) =>
                update({ model: event.target.value })
              }
            />
          </Field>
          <Field label="独立 Base URL">
            <Input
              disabled={!isAdmin}
              value={memory.base_url}
              onChange={(event) =>
                update({ base_url: event.target.value })
              }
            />
          </Field>
          <Field label="最大输出 Token">
            <NumberInput
              value={memory.llm_max_tokens}
              min={1}
              disabled={!isAdmin}
              onChange={(llm_max_tokens) =>
                update({ llm_max_tokens })
              }
            />
          </Field>
          <Field label="Temperature">
            <Input
              type="number"
              min={0}
              max={2}
              step={0.1}
              disabled={!isAdmin}
              value={memory.temperature}
              onChange={(event) =>
                update({
                  temperature: Number(event.target.value),
                })
              }
            />
          </Field>
          <Field label="Customer ID（可选，隔离单客户）">
            <Input
              disabled={!isAdmin}
              value={memory.customer_id || ""}
              onChange={(event) =>
                update({ customer_id: event.target.value })
              }
            />
          </Field>
          <Field
            label={`独立 API Key${memory.api_key_present ? "（已配置，留空保留）" : ""}`}
          >
            <Input
              type="password"
              disabled={!isAdmin}
              value={memory.api_key || ""}
              onChange={(event) =>
                update({ api_key: event.target.value })
              }
            />
          </Field>

          <Field label="活跃开始小时">
            <NumberInput
              value={memory.scheduler.active_start_hour}
              min={0}
              max={23}
              disabled={!isAdmin}
              onChange={(active_start_hour) =>
                updateScheduler({ active_start_hour })
              }
            />
          </Field>
          <Field label="活跃结束小时">
            <NumberInput
              value={memory.scheduler.active_end_hour}
              min={0}
              max={23}
              disabled={!isAdmin}
              onChange={(active_end_hour) =>
                updateScheduler({ active_end_hour })
              }
            />
          </Field>
          <Field label="每窗口轮次">
            <NumberInput
              value={memory.scheduler.rounds_per_window}
              min={1}
              disabled={!isAdmin}
              onChange={(rounds_per_window) =>
                updateScheduler({ rounds_per_window })
              }
            />
          </Field>
          <Field label="轮次间隔（分钟）">
            <NumberInput
              value={memory.scheduler.round_interval_minutes}
              min={1}
              disabled={!isAdmin}
              onChange={(round_interval_minutes) =>
                updateScheduler({ round_interval_minutes })
              }
            />
          </Field>
          <Field label="每 Job 最大 ReAct Turns">
            <NumberInput
              value={memory.scheduler.max_turns_per_job}
              min={1}
              disabled={!isAdmin}
              onChange={(max_turns_per_job) =>
                updateScheduler({ max_turns_per_job })
              }
            />
          </Field>
          <Field label="连续错误上限">
            <NumberInput
              value={memory.scheduler.max_consecutive_errors}
              min={1}
              disabled={!isAdmin}
              onChange={(max_consecutive_errors) =>
                updateScheduler({ max_consecutive_errors })
              }
            />
          </Field>
          <Field label="失败重试等待（秒）">
            <NumberInput
              value={memory.scheduler.retry_delay_seconds}
              min={1}
              disabled={!isAdmin}
              onChange={(retry_delay_seconds) =>
                updateScheduler({ retry_delay_seconds })
              }
            />
          </Field>
        </div>
      </Panel>

      {isAdmin && (
        <Panel
          title="Memory Change / True Replay"
          count={`${memoryChanges.length} 条变更`}
          extra={
            <Pill tone="blue">
              <ShieldCheck className="mr-1 size-3" />
              Snapshot A/B
            </Pill>
          }
        >
          <div className="max-h-[440px] overflow-auto">
            {!memoryChanges.length ? (
              <div className="p-4">
                <Empty>尚无可回放的 Memory Change。</Empty>
              </div>
            ) : (
              <div className="divide-y divide-border">
                {memoryChanges.map((change, index) => {
                  const latest = change.latest_replay;
                  const replayable = ["complete", "partial"].includes(
                    change.snapshot_status || "",
                  );
                  return (
                    <div
                      key={change.change_id}
                      className="grid gap-3 px-4 py-3 lg:grid-cols-[36px_minmax(0,1fr)_minmax(220px,auto)] lg:items-center"
                    >
                      <div className="flex size-7 items-center justify-center rounded-full border border-border bg-surface-subtle text-[11px] font-bold">
                        {index + 1}
                      </div>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <strong className="text-xs">
                            {change.job_name || "DreamCycle"}
                          </strong>
                          <Pill tone="blue">
                            {memoryActionLabel(change.action)}
                          </Pill>
                          <Pill
                            tone={
                              change.snapshot_status === "complete"
                                ? "green"
                                : change.snapshot_status === "partial"
                                  ? "amber"
                                  : "red"
                            }
                          >
                            Snapshot {change.snapshot_status || "unknown"}
                          </Pill>
                          {latest && (
                            <Pill tone={memoryVerdictTone(latest.verdict)}>
                              Replay {latest.verdict || latest.status}
                            </Pill>
                          )}
                        </div>
                        <div
                          className="mt-1 truncate font-mono text-[11px] text-muted-foreground"
                          title={(change.target_paths || []).join("\n")}
                        >
                          {(change.target_paths || [])[0] || change.change_id}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-muted-foreground">
                          <span>before {shortOid(change.before_oid)}</span>
                          <span>after {shortOid(change.after_oid)}</span>
                          <span>{formatTimestamp(change.completed_at)}</span>
                        </div>
                      </div>
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={!replayable}
                          onClick={() => void openMemoryReplay(change)}
                        >
                          <GitCompare className="size-4" />
                          {latest ? "查看 / 重跑" : "运行 True Replay"}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </Panel>
      )}

      <Panel
        title="完整维护 Jobs"
        count={`${memory.jobs.length} 个`}
      >
        <div className="grid gap-4 p-4 xl:grid-cols-2">
          {memory.jobs.map((job) => (
            <DreamCycleJobCard
              key={job.id}
              job={job}
              disabled={!isAdmin}
              onChange={(values) => updateJob(job.id, values)}
            />
          ))}
          {!memory.jobs.length && (
            <div className="xl:col-span-2">
              <Empty>后端未返回 Job Catalog。</Empty>
            </div>
          )}
        </div>
      </Panel>

      {dryRun?.jobs && (
        <Panel
          title="Dry Run 执行计划"
          count={`${dryRun.jobs.length} 个 Job`}
          extra={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setDryRun(null)}
            >
              关闭
            </Button>
          }
        >
          <div className="grid gap-3 p-4 md:grid-cols-2">
            {dryRun.jobs.map((job) => (
              <div
                key={job.id}
                className="rounded-lg border border-border bg-background p-3"
              >
                <div className="flex items-center gap-2">
                  <Pill tone={job.enabled ? "green" : "gray"}>
                    {job.enabled ? "将执行" : "已关闭"}
                  </Pill>
                  <strong>{job.label}</strong>
                </div>
                <ol className="mt-3 list-decimal space-y-1 pl-5 text-xs text-muted-foreground">
                  {job.tasks.map((task) => (
                    <li key={task.id}>{task.description}</li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <Panel
        title="运行历史与报告"
        count={`${status?.history?.length || 0} 轮`}
      >
        <div className="space-y-3 p-4">
          <div className="rounded-lg bg-muted p-3 text-xs text-muted-foreground">
            报告目录：{status?.report_dir || "尚未初始化"}
            {status?.last_error
              ? ` · 最近错误：${status.last_error}`
              : ""}
          </div>
          {status?.last_results?.map((result, index) => (
            <div
              key={`${result.job_name || "job"}-${index}`}
              className="rounded-lg border border-border p-3 text-xs"
            >
              <div className="flex items-center gap-2">
                <Pill
                  tone={
                    result.status === "completed"
                      ? "green"
                      : "red"
                  }
                >
                  {result.status || "unknown"}
                </Pill>
                <strong>{result.job_name}</strong>
                <span className="ml-auto text-muted-foreground">
                  {Number(result.duration_seconds || 0).toFixed(1)}s ·
                  {" "}{result.turns_used || 0} turns ·
                  {" "}{result.actions_taken || 0} actions
                </span>
              </div>
              <pre className="mt-2 whitespace-pre-wrap text-[11px] leading-5 text-muted-foreground">
                {result.summary || "无摘要"}
              </pre>
            </div>
          ))}
          {!status?.last_results?.length && (
            <Empty>尚无完整 DreamCycle 执行结果。</Empty>
          )}
        </div>
      </Panel>

      <Dialog
        open={!!replayChange}
        onOpenChange={(open) => {
          if (!open && !replayRunning) {
            setReplayChange(null);
          }
        }}
      >
        <DialogContent className="flex max-h-[90vh] w-[calc(100vw-32px)] flex-col overflow-hidden !max-w-[1120px] p-0">
          <DialogHeader className="border-b border-border px-5 py-4">
            <DialogTitle className="flex items-center gap-2">
              <GitCompare className="size-4" />
              Memory True Replay
            </DialogTitle>
            <div className="flex flex-wrap items-center gap-2 pt-1 text-[11px] text-muted-foreground">
              <Pill tone="blue">
                {memoryActionLabel(replayChange?.action)}
              </Pill>
              <span>{replayChange?.job_name || "DreamCycle"}</span>
              <span className="font-mono">
                {shortOid(replayChange?.before_oid)} →{" "}
                {shortOid(replayChange?.after_oid)}
              </span>
            </div>
          </DialogHeader>

          <div className="grid min-h-0 flex-1 overflow-auto lg:grid-cols-[360px_minmax(0,1fr)]">
            <div className="space-y-4 border-b border-border p-5 lg:border-b-0 lg:border-r">
              <Field label="Source Session ID（留空自动选择）">
                <Input
                  value={replaySourceSession}
                  onChange={(event) =>
                    setReplaySourceSession(event.target.value)
                  }
                  placeholder="session_id"
                />
              </Field>
              <Field label="Query">
                <Textarea
                  value={replayQuery}
                  onChange={(event) => setReplayQuery(event.target.value)}
                  className="h-[132px] resize-none text-xs leading-5"
                />
              </Field>
              <Field label="Checklist（每行一项）">
                <Textarea
                  value={replayChecklist}
                  onChange={(event) =>
                    setReplayChecklist(event.target.value)
                  }
                  className="h-[156px] resize-none text-xs leading-5"
                />
              </Field>
              <Field label="最大交互轮次">
                <NumberInput
                  value={replayMaxInteractions}
                  min={1}
                  max={20}
                  onChange={setReplayMaxInteractions}
                />
              </Field>
            </div>

            <div className="min-h-[460px] p-5">
              {replayLoading ? (
                <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                  正在加载 Replay 历史…
                </div>
              ) : selectedReplay ? (
                <MemoryReplayResult replay={selectedReplay} />
              ) : (
                <Empty>尚无 Replay 结果。</Empty>
              )}
            </div>
          </div>

          <DialogFooter className="border-t border-border px-5 py-3">
            <div className="mr-auto flex items-center gap-2 text-[11px] text-muted-foreground">
              <History className="size-3.5" />
              {replayHistory.length} 次历史回放
            </div>
            <Button
              variant="outline"
              disabled={replayRunning}
              onClick={() => setReplayChange(null)}
            >
              关闭
            </Button>
            <Button
              disabled={replayRunning}
              onClick={() => void runMemoryReplay()}
            >
              <Play className="size-4" />
              {replayRunning ? "回放运行中…" : "运行 Before / After"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function MemoryReplayResult({ replay }: { replay: MemoryTrueReplay }) {
  const replayCase = replay.cases?.[0] || {};
  const dimensions = replay.efficiency?.dimensions || {};
  const metricRows = [
    ["交互轮次", dimensions.interaction_turns],
    ["工具调用", dimensions.tool_call_count],
    ["Total Tokens", dimensions.total_tokens],
  ] as const;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={memoryVerdictTone(replay.verdict)}>
          {memoryVerdictLabel(replay.verdict)}
        </Pill>
        <Pill tone={replay.status === "evaluated" ? "green" : "red"}>
          {replay.status || "unknown"}
        </Pill>
        <span className="text-[11px] text-muted-foreground">
          {formatTimestamp(replay.completed_at)}
        </span>
      </div>

      <div className="overflow-hidden rounded-lg border border-border">
        <div className="grid grid-cols-[minmax(120px,1fr)_100px_100px_92px] bg-surface-subtle px-3 py-2 text-[10px] font-bold text-muted-foreground">
          <span>指标</span>
          <span>Before</span>
          <span>After</span>
          <span>结果</span>
        </div>
        {metricRows.map(([label, metric]) => (
          <div
            key={label}
            className="grid grid-cols-[minmax(120px,1fr)_100px_100px_92px] border-t border-border px-3 py-2 text-xs"
          >
            <strong>{label}</strong>
            <span>{metric?.baseline ?? "-"}</span>
            <span>{metric?.candidate ?? "-"}</span>
            <span>
              <Pill
                tone={
                  metric?.winner === "candidate"
                    ? "green"
                    : metric?.winner === "baseline"
                      ? "red"
                      : "gray"
                }
              >
                {metric?.winner === "candidate"
                  ? "After"
                  : metric?.winner === "baseline"
                    ? "Before"
                    : "Tie"}
              </Pill>
            </span>
          </div>
        ))}
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <ReplayBranchResult
          title="Before Snapshot"
          branch={replayCase.baseline}
        />
        <ReplayBranchResult
          title="After Snapshot"
          branch={replayCase.candidate}
        />
      </div>

      <div className="rounded-lg border border-border bg-surface-subtle px-3 py-2 text-[11px] leading-5 text-muted-foreground">
        <div>Source Session：{replay.source_session_id || "-"}</div>
        <div>Runtime：{replay.runtime || "-"}</div>
        <div className="truncate" title={replay.reason}>
          结论：{replay.reason || replay.verdict || "-"}
        </div>
      </div>
    </div>
  );
}

function ReplayBranchResult({
  title,
  branch,
}: {
  title: string;
  branch?: MemoryReplayBranch;
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <header className="flex items-center justify-between border-b border-border bg-surface-subtle px-3 py-2 text-xs font-bold">
        <span>{title}</span>
        <Pill tone={branch?.ok ? "green" : "red"}>
          {branch?.ok ? "完成" : "失败"}
        </Pill>
      </header>
      <div className="grid grid-cols-3 gap-2 border-b border-border px-3 py-2 text-center text-[10px]">
        <div>
          <div className="font-bold">{branch?.interaction_turns ?? "-"}</div>
          <div className="text-muted-foreground">轮次</div>
        </div>
        <div>
          <div className="font-bold">{branch?.tool_call_count ?? "-"}</div>
          <div className="text-muted-foreground">工具</div>
        </div>
        <div>
          <div className="font-bold">{branch?.total_tokens ?? "-"}</div>
          <div className="text-muted-foreground">Tokens</div>
        </div>
      </div>
      <pre className="h-[150px] overflow-auto whitespace-pre-wrap px-3 py-2 text-[11px] leading-5 text-muted-foreground">
        {branch?.error || branch?.final_response || "无输出"}
      </pre>
    </section>
  );
}

function memoryActionLabel(action?: string): string {
  const labels: Record<string, string> = {
    create: "新增",
    update: "更新",
    merge: "合并",
    archive: "归档",
    sanitize: "清洗",
  };
  return labels[action || ""] || action || "变更";
}

function memoryVerdictTone(
  verdict?: MemoryTrueReplay["verdict"],
): PillTone {
  if (verdict === "accept") return "green";
  if (verdict === "reject") return "red";
  return "amber";
}

function memoryVerdictLabel(
  verdict?: MemoryTrueReplay["verdict"],
): string {
  if (verdict === "accept") return "After 胜出";
  if (verdict === "reject") return "Before 胜出";
  return "结论不明确";
}

function shortOid(value?: string): string {
  return value ? value.slice(0, 10) : "-";
}

function formatTimestamp(value?: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString();
}

function DreamCycleJobCard({
  job,
  disabled,
  onChange,
}: {
  job: DreamCycleJob;
  disabled: boolean;
  onChange: (values: Partial<DreamCycleJob>) => void;
}) {
  const [showPlan, setShowPlan] = useState(false);
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-background">
      <header className="flex items-start justify-between gap-3 border-b border-line bg-surface-subtle p-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={job.enabled ? "green" : "gray"}>
              P{job.priority}
            </Pill>
            <strong>{job.label}</strong>
            {job.overridden && <Pill tone="amber">Prompt 已改</Pill>}
          </div>
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {job.description}
          </p>
        </div>
        <input
          type="checkbox"
          checked={job.enabled}
          disabled={disabled}
          onChange={(event) =>
            onChange({ enabled: event.target.checked })
          }
        />
      </header>
      <div className="space-y-3 p-3">
        <button
          type="button"
          className="text-xs font-semibold text-muted-foreground underline"
          onClick={() => setShowPlan((current) => !current)}
        >
          {showPlan ? "隐藏" : "查看"} TaskPlan（{job.tasks.length}）
        </button>
        {showPlan && (
          <ol className="list-decimal space-y-1 rounded-lg bg-muted p-3 pl-8 text-[11px] leading-5">
            {job.tasks.map((task) => (
              <li key={task.id}>
                <span className="font-semibold">{task.priority}</span>
                {" · "}
                {task.description}
              </li>
            ))}
          </ol>
        )}
        <Textarea
          value={job.effective_prompt}
          disabled={disabled}
          onChange={(event) =>
            onChange({
              effective_prompt: event.target.value,
              overridden:
                event.target.value !== job.default_prompt,
            })
          }
          className="h-[280px] min-h-[280px] resize-y font-mono text-[11px] leading-5"
        />
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span>{job.effective_prompt.length} 字符</span>
          <Button
            variant="ghost"
            size="sm"
            disabled={disabled || !job.overridden}
            onClick={() =>
              onChange({
                effective_prompt: job.default_prompt,
                overridden: false,
              })
            }
          >
            <RotateCcw className="size-3.5" />
            恢复默认
          </Button>
        </div>
      </div>
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="text-xs font-semibold text-muted-foreground">
      {label}
      <div className="mt-1.5">{children}</div>
    </label>
  );
}

function NumberInput({
  value,
  onChange,
  disabled,
  min,
  max,
}: {
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  min?: number;
  max?: number;
}) {
  return (
    <Input
      type="number"
      min={min}
      max={max}
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(Number(event.target.value))}
    />
  );
}

function ToggleField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex items-center justify-between rounded-lg border border-border bg-background px-3 py-2.5 text-xs font-semibold">
      <span>{label}</span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
    </label>
  );
}
