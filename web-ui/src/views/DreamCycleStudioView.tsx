import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Brain,
  ClipboardList,
  Database,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Users,
} from "lucide-react";

import {
  api,
  type EvolveProcessSettings,
  type UserProfile,
} from "@/api/client";
import { Empty, Panel, Pill, StatCard } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

type MemorySettings = EvolveProcessSettings["memory_maintenance"];
type DreamCycleJob = MemorySettings["jobs"][number];
type JobRuntime = DreamCycleJob["runtime"];

type DreamCycleStatus = {
  engine?: string;
  full_capabilities?: boolean;
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
  agent_id?: string;
  customer_id?: string;
  maintained_space?: string;
  semantic_dedup_enabled?: boolean;
  tools?: string[];
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
      agent_id: memory.agent_id || "",
      customer_id: memory.customer_id || "",
      maintained_space:
        memory.maintained_space || "viking://user/memories/",
      embed_model: memory.embed_model || "",
      embed_base_url: memory.embed_base_url || "",
      semantic_dedup_enabled: Boolean(
        memory.semantic_dedup_enabled,
      ),
      dedup_merge_threshold: Number(
        memory.dedup_merge_threshold ?? 0.86,
      ),
      dedup_warn_threshold: Number(
        memory.dedup_warn_threshold ?? 0.72,
      ),
      tools: Array.isArray(memory.tools) ? memory.tools : [],
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
      jobs: Array.isArray(memory.jobs)
        ? memory.jobs.map((job) => ({
            ...job,
            runtime: {
              model: job.runtime?.model || "",
              base_url: job.runtime?.base_url || "",
              temperature: Number(
                job.runtime?.temperature
                  ?? memory.temperature
                  ?? 0.3,
              ),
              max_tokens: Number(
                job.runtime?.max_tokens
                  || memory.llm_max_tokens
                  || 4096,
              ),
              max_turns: Number(
                job.runtime?.max_turns
                  || memory.scheduler?.max_turns_per_job
                  || 25,
              ),
              max_errors: Number(
                job.runtime?.max_errors
                  || memory.scheduler?.max_consecutive_errors
                  || 3,
              ),
            },
            default_runtime: job.default_runtime || {
              model: "",
              base_url: "",
              temperature: Number(memory.temperature ?? 0.3),
              max_tokens: Number(memory.llm_max_tokens || 4096),
              max_turns: Number(
                memory.scheduler?.max_turns_per_job || 25,
              ),
              max_errors: Number(
                memory.scheduler?.max_consecutive_errors || 3,
              ),
            },
          }))
        : [],
    },
  };
}

export default function DreamCycleStudioView({
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
  const [selectedJobId, setSelectedJobId] = useState("");
  const [showDefault, setShowDefault] = useState(false);
  const [dryRunJobs, setDryRunJobs] =
    useState<DreamCycleJob[] | null>(null);
  const [resetPreview, setResetPreview] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [config, nextStatus] = await Promise.all([
        api<EvolveProcessSettings>("/api/evolve-settings"),
        api<DreamCycleStatus>("/trigger-dreamcycle/status"),
      ]);
      const normalized = normalizeSettings(config);
      setSettings(normalized);
      setStatus(nextStatus);
      setSelectedJobId((current) => (
        normalized.memory_maintenance.jobs.some(
          (job) => job.id === current,
        )
          ? current
          : normalized.memory_maintenance.jobs[0]?.id || ""
      ));
    } catch (error: any) {
      toastErr("加载 DreamCycle 失败", error.message);
    } finally {
      setLoading(false);
    }
  }, []);

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

  const memory = settings?.memory_maintenance;
  const selectedJob = useMemo(
    () => memory?.jobs.find((job) => job.id === selectedJobId) || null,
    [memory?.jobs, selectedJobId],
  );
  const selectedResult = status?.last_results?.find(
    (result) => result.job_name === selectedJobId,
  );

  if (!settings || !memory) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        {loading ? "正在加载 DreamCycle…" : "DreamCycle 配置不可用"}
      </div>
    );
  }

  const updateMemory = (values: Partial<MemorySettings>) => {
    setSettings({
      ...settings,
      memory_maintenance: { ...memory, ...values },
    });
  };
  const updateJob = (
    jobId: string,
    values: Partial<DreamCycleJob>,
  ) => {
    updateMemory({
      jobs: memory.jobs.map((job) =>
        job.id === jobId ? { ...job, ...values } : job
      ),
    });
  };
  const updateRuntime = (values: Partial<JobRuntime>) => {
    if (!selectedJob) return;
    const runtime = {
      ...selectedJob.runtime,
      ...values,
    };
    updateJob(selectedJob.id, {
      runtime,
      settings_overridden: !sameRuntime(
        runtime,
        selectedJob.default_runtime,
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
        "DreamCycle 阶段配置已保存",
        "Prompt、模型、ReAct 参数与调度策略已生效",
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
      setDryRunJobs(result.jobs || []);
      toastOk("Dry Run 已生成", `${result.jobs?.length || 0} 个 Job`);
    } catch (error: any) {
      toastErr("生成 Dry Run 失败", error.message);
    }
  }

  async function inspectReset() {
    try {
      const result = await api<{ output: string }>(
        "/trigger-dreamcycle/reset",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            remote: true,
            dry_run: true,
          }),
        },
      );
      setResetPreview(result.output || "");
      toastOk("Reset 预览已生成", "未执行写入或归档");
    } catch (error: any) {
      toastErr("生成 Reset 预览失败", error.message);
    }
  }

  return (
    <div className="mx-auto max-w-[1440px] space-y-5 px-[22px] py-[22px]">
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
            每个 Job 的 Prompt、模型和 ReAct 参数独立可调；未覆盖时继承全局默认值。
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
            onClick={inspectReset}
            disabled={!isAdmin}
          >
            <RotateCcw className="size-4" />
            Reset 预览
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={trigger}
            disabled={!isAdmin || running || status?.running}
          >
            <Play className="size-4" />
            {running || status?.running ? "运行中…" : "运行完整链路"}
          </Button>
          <Button
            size="sm"
            onClick={save}
            disabled={!isAdmin || saving}
          >
            <Save className="size-4" />
            保存并生效
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
          label="维护身份"
          value={status?.agent_id || memory.agent_id || "未解析"}
        />
        <StatCard
          label="语义去重"
          value={
            status?.semantic_dedup_enabled
            || memory.semantic_dedup_enabled
              ? "Embedding 已启用"
              : "Embedding 未配置"
          }
        />
        <StatCard
          label="活跃窗口"
          value={`${memory.scheduler.active_start_hour}:00-${memory.scheduler.active_end_hour}:00`}
        />
      </div>

      <Panel title="团队 Memory 进化链路">
        <div className="flex flex-wrap items-stretch gap-2 p-4">
          {memory.jobs.map((job, index) => (
            <div key={job.id} className="flex items-stretch gap-2">
              <button
                type="button"
                onClick={() => {
                  setSelectedJobId(job.id);
                  setShowDefault(false);
                }}
                className={cn(
                  "flex w-[190px] flex-col gap-1 rounded-lg border p-3 text-left",
                  selectedJobId === job.id
                    ? "border-sidebar-primary bg-accent-soft"
                    : "border-border bg-surface-subtle hover:border-sidebar-primary",
                  !job.enabled && "opacity-55",
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <Pill tone={job.enabled ? "blue" : "gray"}>
                    Job P{job.priority}
                  </Pill>
                  {(job.overridden || job.settings_overridden) && (
                    <Pill tone="amber">已调整</Pill>
                  )}
                </span>
                <strong className="text-xs">{job.label}</strong>
                <span className="line-clamp-2 text-[10px] leading-4 text-muted-foreground">
                  {job.description}
                </span>
              </button>
              {index < memory.jobs.length - 1 && (
                <span className="flex items-center text-muted-soft">→</span>
              )}
            </div>
          ))}
          {!memory.jobs.length && (
            <Empty>
              后端未返回完整 Job Catalog，请重启 teamEvolver 服务。
            </Empty>
          )}
        </div>
      </Panel>

      <div className="grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
        <div>
          <Panel title="进化阶段" count={`${memory.jobs.length} 个`}>
            <div className="flex flex-col p-2">
              {memory.jobs.map((job) => (
                <button
                  key={job.id}
                  type="button"
                  onClick={() => {
                    setSelectedJobId(job.id);
                    setShowDefault(false);
                  }}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left text-xs",
                    selectedJobId === job.id
                      ? "bg-sidebar-accent font-semibold"
                      : "hover:bg-muted",
                  )}
                >
                  <span className="truncate">{job.label}</span>
                  <Pill tone={job.enabled ? "green" : "gray"}>
                    {job.enabled ? "启用" : "关闭"}
                  </Pill>
                </button>
              ))}
            </div>
            <div className="space-y-3 border-t border-line p-3">
              <div className="text-xs font-bold text-muted-foreground">
                数据源与写入目标
              </div>
              <SourceCard
                icon={Users}
                title="多源 Memory"
                detail="跨已配置用户检索，只读"
                tone="blue"
              />
              <SourceCard
                icon={Brain}
                title="维护目标"
                detail={memory.maintained_space || "viking://user/memories/"}
                tone="purple"
              />
              <SourceCard
                icon={Database}
                title="OpenViking 工具"
                detail={`${memory.tools?.length || 12} 个工具，含批量读取、合并与共享笔记`}
                tone="gray"
              />
              <div className="flex flex-wrap gap-1.5">
                {memory.tools?.map((tool) => (
                  <Pill key={tool} tone="gray">{tool}</Pill>
                ))}
              </div>
            </div>
          </Panel>
        </div>

        <div className="space-y-5">
          {!selectedJob ? (
            <Panel title="DreamCycle Job 编辑器">
              <Empty>从左侧选择一个进化 Job。</Empty>
            </Panel>
          ) : (
            <>
              <Panel
                title={
                  <span className="flex flex-wrap items-center gap-2">
                    {selectedJob.label}
                    <Pill tone={selectedJob.enabled ? "green" : "gray"}>
                      {selectedJob.enabled ? "启用" : "关闭"}
                    </Pill>
                    {(selectedJob.overridden
                      || selectedJob.settings_overridden) && (
                      <Pill tone="amber">自定义</Pill>
                    )}
                  </span>
                }
                extra={
                  <div className="flex gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setShowDefault((current) => !current)}
                    >
                      {showDefault ? "隐藏默认" : "对照默认"}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!isAdmin}
                      onClick={() => updateJob(selectedJob.id, {
                        effective_prompt: selectedJob.default_prompt,
                        runtime: selectedJob.default_runtime,
                        overridden: false,
                        settings_overridden: false,
                      })}
                    >
                      <RotateCcw className="size-3.5" />
                      全部恢复默认
                    </Button>
                  </div>
                }
              >
                <div className="space-y-4 p-4">
                  <p className="text-xs leading-5 text-muted-foreground">
                    {selectedJob.description}
                  </p>
                  <ToggleField
                    label="启用该进化 Job"
                    checked={selectedJob.enabled}
                    disabled={!isAdmin}
                    onChange={(enabled) =>
                      updateJob(selectedJob.id, { enabled })
                    }
                  />

                  <div className="rounded-lg border border-border bg-background/60 p-3">
                    <div className="mb-1 text-xs font-bold">
                      阶段模型与 ReAct 参数
                    </div>
                    <div className="mb-3 text-[11px] text-muted-foreground">
                      与 Skills 自进化一致：参数和 Prompt
                      一起保存；模型与 Base URL 留空时继承 DreamCycle 全局值。
                    </div>
                    <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                      <Field label="阶段模型">
                        <Input
                          disabled={!isAdmin}
                          value={selectedJob.runtime.model}
                          placeholder={memory.model || "继承全局模型"}
                          onChange={(event) =>
                            updateRuntime({ model: event.target.value })
                          }
                        />
                      </Field>
                      <Field label="阶段 Base URL">
                        <Input
                          disabled={!isAdmin}
                          value={selectedJob.runtime.base_url}
                          placeholder={memory.base_url || "继承全局 Base URL"}
                          onChange={(event) =>
                            updateRuntime({ base_url: event.target.value })
                          }
                        />
                      </Field>
                      <Field label="Temperature">
                        <NumberInput
                          value={selectedJob.runtime.temperature}
                          min={0}
                          max={2}
                          step={0.1}
                          disabled={!isAdmin}
                          onChange={(temperature) =>
                            updateRuntime({ temperature })
                          }
                        />
                      </Field>
                      <Field label="最大输出 Token">
                        <NumberInput
                          value={selectedJob.runtime.max_tokens}
                          min={1}
                          disabled={!isAdmin}
                          onChange={(max_tokens) =>
                            updateRuntime({ max_tokens })
                          }
                        />
                      </Field>
                      <Field label="最大 ReAct Turns">
                        <NumberInput
                          value={selectedJob.runtime.max_turns}
                          min={1}
                          disabled={!isAdmin}
                          onChange={(max_turns) =>
                            updateRuntime({ max_turns })
                          }
                        />
                      </Field>
                      <Field label="连续错误上限">
                        <NumberInput
                          value={selectedJob.runtime.max_errors}
                          min={1}
                          disabled={!isAdmin}
                          onChange={(max_errors) =>
                            updateRuntime({ max_errors })
                          }
                        />
                      </Field>
                    </div>
                  </div>

                  <div className="rounded-lg border border-border bg-background/60 p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="text-xs font-bold">
                        TaskPlan · {selectedJob.tasks.length} 项
                      </div>
                      <Pill tone="purple">
                        优先级 P{selectedJob.priority}
                      </Pill>
                    </div>
                    <ol className="grid list-decimal gap-2 pl-5 text-[11px] leading-5 md:grid-cols-2">
                      {selectedJob.tasks.map((task) => (
                        <li key={task.id}>
                          <span className="font-semibold">{task.priority}</span>
                          {" · "}
                          {task.description}
                        </li>
                      ))}
                    </ol>
                  </div>

                  <div
                    className={cn(
                      "grid gap-3",
                      showDefault && "xl:grid-cols-2",
                    )}
                  >
                    <PromptEditor
                      title="当前 Job System Prompt（可编辑）"
                      value={selectedJob.effective_prompt}
                      disabled={!isAdmin}
                      onChange={(effective_prompt) =>
                        updateJob(selectedJob.id, {
                          effective_prompt,
                          overridden:
                            effective_prompt
                            !== selectedJob.default_prompt,
                        })
                      }
                    />
                    {showDefault && (
                      <PromptEditor
                        title="默认 Job System Prompt（只读）"
                        value={selectedJob.default_prompt}
                        disabled
                      />
                    )}
                  </div>
                </div>
              </Panel>

              <Panel
                title="阶段测试与运行结果"
                extra={
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!isAdmin}
                    onClick={inspectPlan}
                  >
                    <ClipboardList className="size-3.5" />
                    Dry Run 全链路
                  </Button>
                }
              >
                <div className="space-y-3 p-4">
                  <div className="grid gap-3 md:grid-cols-3">
                    <Info
                      label="读取范围"
                      value="认证用户 + 已配置只读来源"
                    />
                    <Info
                      label="工具权限"
                      value="仅认证用户维护空间写入 / 归档"
                    />
                    <Info
                      label="执行预算"
                      value={`${selectedJob.runtime.max_turns} turns / ${selectedJob.runtime.max_errors} errors`}
                    />
                  </div>
                  {selectedResult ? (
                    <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-background p-3 text-[11px] leading-5">
                      {selectedResult.summary || JSON.stringify(selectedResult, null, 2)}
                    </pre>
                  ) : (
                    <Empty>该 Job 尚无最近运行结果。</Empty>
                  )}
                </div>
              </Panel>
            </>
          )}
        </div>
      </div>

      <Panel
        title="DreamCycle 全局运行策略"
        extra={
          <Pill
            tone={
              status?.daemon_running
                ? "green"
                : memory.auto_start
                  ? "amber"
                  : "gray"
            }
          >
            {status?.daemon_running
              ? "调度中"
              : memory.auto_start
                ? "等待服务重载"
                : "手动运行"}
          </Pill>
        }
      >
        <div className="grid gap-4 p-4 md:grid-cols-2 lg:grid-cols-4">
          <ToggleField
            label="启用完整 DreamCycle"
            checked={memory.enabled}
            disabled={!isAdmin}
            onChange={(enabled) => updateMemory({ enabled })}
          />
          <ToggleField
            label="启用夜间自动调度"
            checked={memory.auto_start}
            disabled={!isAdmin}
            onChange={(auto_start) => updateMemory({ auto_start })}
          />
          <Field label="全局模型">
            <Input
              disabled={!isAdmin}
              value={memory.model}
              placeholder="留空继承平台全局模型"
              onChange={(event) =>
                updateMemory({ model: event.target.value })
              }
            />
          </Field>
          <Field label="全局 Base URL">
            <Input
              disabled={!isAdmin}
              value={memory.base_url}
              onChange={(event) =>
                updateMemory({ base_url: event.target.value })
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
                updateMemory({
                  scheduler: {
                    ...memory.scheduler,
                    active_start_hour,
                  },
                })
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
                updateMemory({
                  scheduler: {
                    ...memory.scheduler,
                    active_end_hour,
                  },
                })
              }
            />
          </Field>
          <Field label="每窗口轮次">
            <NumberInput
              value={memory.scheduler.rounds_per_window}
              min={1}
              disabled={!isAdmin}
              onChange={(rounds_per_window) =>
                updateMemory({
                  scheduler: {
                    ...memory.scheduler,
                    rounds_per_window,
                  },
                })
              }
            />
          </Field>
          <Field label="轮次间隔（分钟）">
            <NumberInput
              value={memory.scheduler.round_interval_minutes}
              min={1}
              disabled={!isAdmin}
              onChange={(round_interval_minutes) =>
                updateMemory({
                  scheduler: {
                    ...memory.scheduler,
                    round_interval_minutes,
                  },
                })
              }
            />
          </Field>
          <Field label="Customer ID（可选）">
            <Input
              disabled={!isAdmin}
              value={memory.customer_id || ""}
              placeholder="留空维护认证用户自身 Memory"
              onChange={(event) =>
                updateMemory({ customer_id: event.target.value })
              }
            />
          </Field>
          <Field label="失败重试等待（秒）">
            <NumberInput
              value={memory.scheduler.retry_delay_seconds}
              min={1}
              disabled={!isAdmin}
              onChange={(retry_delay_seconds) =>
                updateMemory({
                  scheduler: {
                    ...memory.scheduler,
                    retry_delay_seconds,
                  },
                })
              }
            />
          </Field>
          <Field label="Embedding 模型">
            <Input
              disabled={!isAdmin}
              value={memory.embed_model || ""}
              placeholder="配置后启用语义去重"
              onChange={(event) =>
                updateMemory({ embed_model: event.target.value })
              }
            />
          </Field>
          <Field label="Embedding Base URL">
            <Input
              disabled={!isAdmin}
              value={memory.embed_base_url || ""}
              placeholder="留空继承全局 Base URL"
              onChange={(event) =>
                updateMemory({ embed_base_url: event.target.value })
              }
            />
          </Field>
          <Field
            label={`Embedding API Key${memory.embed_api_key_present ? "（已配置，留空保留）" : ""}`}
          >
            <Input
              type="password"
              disabled={!isAdmin}
              value={memory.embed_api_key || ""}
              onChange={(event) =>
                updateMemory({ embed_api_key: event.target.value })
              }
            />
          </Field>
          <Field label="语义合并阈值">
            <NumberInput
              value={memory.dedup_merge_threshold ?? 0.86}
              min={-1}
              max={1}
              step={0.01}
              disabled={!isAdmin}
              onChange={(dedup_merge_threshold) =>
                updateMemory({ dedup_merge_threshold })
              }
            />
          </Field>
          <Field label="语义提醒阈值">
            <NumberInput
              value={memory.dedup_warn_threshold ?? 0.72}
              min={-1}
              max={1}
              step={0.01}
              disabled={!isAdmin}
              onChange={(dedup_warn_threshold) =>
                updateMemory({ dedup_warn_threshold })
              }
            />
          </Field>
        </div>
      </Panel>

      {dryRunJobs && (
        <Panel
          title="Dry Run 可视化"
          count={`${dryRunJobs.length} 个 Job`}
          extra={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setDryRunJobs(null)}
            >
              关闭
            </Button>
          }
        >
          <div className="grid gap-3 p-4 md:grid-cols-2">
            {dryRunJobs.map((job) => (
              <div
                key={job.id}
                className="rounded-lg border border-border p-3"
              >
                <div className="flex items-center gap-2">
                  <Pill tone={job.enabled ? "green" : "gray"}>
                    {job.enabled ? "将执行" : "已关闭"}
                  </Pill>
                  <strong>{job.label}</strong>
                </div>
                <ol className="mt-3 list-decimal space-y-1 pl-5 text-[11px]">
                  {job.tasks.map((task) => (
                    <li key={task.id}>{task.description}</li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {resetPreview && (
        <Panel
          title="Reset Dry Run"
          extra={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setResetPreview("")}
            >
              关闭
            </Button>
          }
        >
          <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 text-[11px] leading-5">
            {resetPreview}
          </pre>
        </Panel>
      )}
    </div>
  );
}

function sameRuntime(a: JobRuntime, b: JobRuntime): boolean {
  return (
    a.model === b.model
    && a.base_url === b.base_url
    && a.temperature === b.temperature
    && a.max_tokens === b.max_tokens
    && a.max_turns === b.max_turns
    && a.max_errors === b.max_errors
  );
}

function PromptEditor({
  title,
  value,
  onChange,
  disabled,
}: {
  title: string;
  value: string;
  onChange?: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
        {title}
      </div>
      <Textarea
        value={value}
        readOnly={disabled}
        disabled={disabled}
        onChange={(event) => onChange?.(event.target.value)}
        className="h-[440px] min-h-[440px] resize-y font-mono text-[11px] leading-5"
      />
      <div className="mt-1 text-[10px] text-muted-foreground">
        {value.length} 字符
      </div>
    </div>
  );
}

function SourceCard({
  icon: Icon,
  title,
  detail,
  tone,
}: {
  icon: typeof Users;
  title: string;
  detail: string;
  tone: "blue" | "purple" | "gray";
}) {
  return (
    <div className="rounded-lg border border-border bg-background p-2.5">
      <div className="flex items-center gap-2">
        <Icon className="size-4" />
        <Pill tone={tone}>{title}</Pill>
      </div>
      <div className="mt-1.5 text-[10px] leading-4 text-muted-foreground">
        {detail}
      </div>
    </div>
  );
}

function Info({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="text-[10px] font-semibold text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-xs font-semibold">{value}</div>
    </div>
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
  step,
}: {
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <Input
      type="number"
      min={min}
      max={max}
      step={step}
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
