import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle, CalendarClock, CheckCheck, Clock3, Code2, DownloadCloud,
  FilePlus2, List, Play, PlugZap, RefreshCw, Save, Upload, X,
} from "lucide-react";
import { api, type UserProfile } from "@/api/client";
import { Empty, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toastErr, toastOk } from "@/lib/toast";

interface AdapterFile {
  file: string;
  label?: string;
  provider?: string;
  host?: string;
  project_id?: string;
  bound_tenant_id?: string;
  error?: string;
  supported_filters?: string[];
  required_filters?: string[];
  max_sessions?: number;
}

interface Persistence {
  durable: boolean;
  mode: "runtime_only";
  warning: string;
}

interface Datasource extends AdapterFile {
  tenant_id: string;
  configured: boolean;
  enabled: boolean;
  revision?: string;
  available: AdapterFile[];
  persistence?: Persistence;
  schedule?: DatasourceSchedule;
  schedule_status?: DatasourceScheduleStatus;
}

interface DatasourceSchedule {
  enabled: boolean;
  time: string;
  timezone: string;
  window: "previous_day";
  max_sessions: number;
}

interface DatasourceScheduleStatus {
  running: boolean;
  next_run_at?: string;
  last_started_at?: string;
  last_finished_at?: string;
  last_status?: string;
  last_error?: string;
  last_window_from?: string;
  last_window_to?: string;
  last_target_date?: string;
  last_total?: number;
  last_counts?: Record<string, number>;
}

interface SessionRow {
  session_id: string;
  title?: string;
  user_id?: string;
  timestamp?: string;
  trace_count?: number;
  status?: string;
  reason?: string;
}

interface DraftResult {
  ok: boolean;
  metadata: AdapterFile;
  health?: { ok: boolean; error?: string };
  sessions?: SessionRow[];
  count?: number;
  [key: string]: unknown;
}

const LABELS: Record<string, string> = {
  from_timestamp: "起始时间", to_timestamp: "结束时间",
  session_id: "Session ID", user_id: "用户", environment: "Environment",
  tags: "Tags", trace_name: "Trace 名称", release: "Release",
  version: "Version", metadata: "Metadata",
};

const DEFAULT_SCHEDULE: DatasourceSchedule = {
  enabled: false,
  time: "00:00",
  timezone: "Asia/Shanghai",
  window: "previous_day",
  max_sessions: 1000,
};

const SCHEDULE_TIMEZONES = [
  "Asia/Shanghai",
  "UTC",
  "Asia/Tokyo",
  "Europe/London",
  "America/Los_Angeles",
];

function formatScheduleTime(value: string | undefined, timezone: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  } catch {
    return parsed.toLocaleString();
  }
}

// datetime-local 输入值（本地时间）转 UTC ISO 8601，供后端使用
function toIsoUtc(localValue: string): string {
  const d = new Date(localValue);
  return isNaN(d.getTime()) ? localValue : d.toISOString();
}

const WARNING =
  "当前修改只写入本实例的运行目录，重新部署、替换容器或重新安装后可能丢失。"
  + "验证通过后请联系项目 Owner，将适配器变更合入源码并重新发布。";

const TEMPLATE = `"""Tenant upstream datasource adapter."""

SOURCE = {
    "label": "New tenant adapter",
    "provider": "custom",
    "host": "",
    "enabled": True,
    "max_sessions": 100,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id"],
    "required_filters": [],
}

def build_adapter():
    return Adapter()

class Adapter:
    def health(self):
        return {"ok": False, "error": "Implement the upstream health probe"}

    def list_session_ids(self, filters, *, max_sessions):
        raise NotImplementedError

    def fetch_session(self, session_id):
        raise NotImplementedError

    def convert_session(self, raw_session, raw_traces):
        raise NotImplementedError

    def close(self):
        pass
`;

export default function DataSourcesView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const uploadRef = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState<Datasource | null>(null);
  const [file, setFile] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [limit, setLimit] = useState("100");
  const [health, setHealth] = useState<{ ok: boolean; error?: string } | null>(null);
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [codeFile, setCodeFile] = useState("");
  const [openedFile, setOpenedFile] = useState("");
  const [revision, setRevision] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [draftMeta, setDraftMeta] = useState<AdapterFile | null>(null);
  const [draftResult, setDraftResult] = useState<Record<string, unknown> | null>(null);
  const [schedule, setSchedule] = useState<DatasourceSchedule>(DEFAULT_SCHEDULE);
  const [schedulePolling, setSchedulePolling] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async (selected?: string) => {
    const data = await api<Datasource>("/api/datasource");
    setSource(data);
    setFile(selected ?? data.file);
    setHealth(null);
    setRows(null);
    setCounts(null);
    setSchedule(data.schedule || DEFAULT_SCHEDULE);
    setSchedulePolling(Boolean(data.schedule_status?.running));
    setError("");
  }, []);

  const refreshScheduleStatus = useCallback(async () => {
    const data = await api<{
      schedule: DatasourceSchedule;
      schedule_status: DatasourceScheduleStatus;
    }>("/api/datasource/schedule");
    setSource((current) => current
      ? {
          ...current,
          schedule: data.schedule,
          schedule_status: data.schedule_status,
        }
      : current);
    if (!data.schedule_status.running) setSchedulePolling(false);
  }, []);

  useEffect(() => {
    if (active && isAdmin) refresh().catch((e: any) => setError(e.message));
  }, [active, isAdmin, refresh]);

  useEffect(() => {
    if (!active || !isAdmin || (!schedulePolling && !source?.schedule_status?.running)) return;
    const timer = window.setInterval(() => {
      refreshScheduleStatus().catch((e: any) => setError(e.message));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [
    active,
    isAdmin,
    refreshScheduleStatus,
    schedulePolling,
    source?.schedule_status?.running,
  ]);

  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  function mayDiscard() {
    return !dirty || window.confirm("当前适配器有未保存修改，确认放弃？");
  }

  function closeDraft() {
    setCode(null);
    setCodeFile("");
    setOpenedFile("");
    setRevision(null);
    setDirty(false);
    setDraftMeta(null);
    setDraftResult(null);
  }

  function chooseFile(value: string) {
    if (!mayDiscard()) return;
    closeDraft();
    setFile(value);
  }

  function filterBody(meta: AdapterFile | null = source) {
    const body: Record<string, unknown> = {};
    for (const key of meta?.supported_filters || []) {
      const value = filters[key]?.trim();
      if (!value) continue;
      body[key] = key === "metadata"
        ? JSON.parse(value)
        : ["tags", "environment"].includes(key)
          ? value.split(",").map((item) => item.trim()).filter(Boolean)
          : key.endsWith("timestamp")
            ? toIsoUtc(value)
            : value;
    }
    return body;
  }

  async function loadCode() {
    if (!file) return toastErr("请先选择适配器文件");
    if (!mayDiscard()) return;
    setBusy("loadCode");
    try {
      const data = await api<{
        file: string;
        code: string;
        revision: string;
      }>(`/api/datasource/code?file=${encodeURIComponent(file)}`);
      setCode(data.code);
      setCodeFile(data.file);
      setOpenedFile(data.file);
      setRevision(data.revision);
      setDirty(false);
      setDraftMeta(source?.available.find((item) => item.file === data.file) || null);
      setDraftResult(null);
    } catch (e: any) {
      toastErr("加载适配器失败", e.message);
    } finally {
      setBusy("");
    }
  }

  function newDraft() {
    if (!mayDiscard()) return;
    setCode(TEMPLATE);
    setCodeFile("tenant-adapter.py");
    setOpenedFile("");
    setRevision(null);
    setDirty(true);
    setDraftMeta(null);
    setDraftResult(null);
  }

  function readUpload(uploaded: File): Promise<string> {
    if (typeof uploaded.text === "function") return uploaded.text();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("读取文件失败"));
      reader.readAsText(uploaded);
    });
  }

  async function uploadDraft(uploaded?: File) {
    if (!uploaded) return;
    if (!uploaded.name.endsWith(".py")) return toastErr("仅支持 .py 适配器文件");
    if (uploaded.size > 256 * 1024) return toastErr("适配器文件超过 256 KiB");
    if (!mayDiscard()) return;
    setCode(await readUpload(uploaded));
    setCodeFile(uploaded.name);
    setOpenedFile("");
    setRevision(null);
    setDirty(true);
    setDraftMeta(null);
    setDraftResult(null);
    if (uploadRef.current) uploadRef.current.value = "";
  }

  async function saveBinding() {
    setBusy("binding");
    try {
      await api("/api/datasource", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file }),
      });
      closeDraft();
      setFilters({});
      await refresh();
      toastOk("适配器绑定已保存");
    } catch (e: any) {
      setError(e.message);
      toastErr("保存绑定失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function saveSchedule() {
    if (!Number.isInteger(schedule.max_sessions)
      || schedule.max_sessions < 1
      || schedule.max_sessions > 1000) {
      return toastErr("单次最大 Session 数必须为 1 到 1000");
    }
    setBusy("schedule-save");
    try {
      const result = await api<{
        schedule: DatasourceSchedule;
        schedule_status: DatasourceScheduleStatus;
      }>("/api/datasource/schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(schedule),
      });
      setSchedule(result.schedule);
      setSource((current) => current
        ? {
            ...current,
            schedule: result.schedule,
            schedule_status: result.schedule_status,
          }
        : current);
      toastOk(result.schedule.enabled ? "定时拉取已启用" : "定时拉取已停用");
    } catch (e: any) {
      setError(e.message);
      toastErr("保存定时配置失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function runScheduleNow() {
    setBusy("schedule-run");
    try {
      const result = await api<{
        accepted: boolean;
        schedule_status: DatasourceScheduleStatus;
      }>("/api/datasource/schedule/run", { method: "POST" });
      setSource((current) => current
        ? { ...current, schedule_status: result.schedule_status }
        : current);
      setSchedulePolling(true);
      toastOk("昨日 Trace 拉取已启动", "拉取结果将进入 Session 队列");
    } catch (e: any) {
      setError(e.message);
      toastErr("启动定时拉取失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function saveCode() {
    if (code === null) return;
    setBusy("saveCode");
    try {
      const saved = await api<{ file: string; revision: string }>(
        "/api/datasource/code",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            file: codeFile.trim(),
            code,
            expected_revision: codeFile.trim() === openedFile ? revision : null,
          }),
        },
      );
      setCodeFile(saved.file);
      setOpenedFile(saved.file);
      setRevision(saved.revision);
      setDirty(false);
      setFile(saved.file);
      await refresh(saved.file);
      toastOk(
        "运行副本已保存",
        saved.file === source?.file
          ? "下次操作立即使用新版本"
          : "该文件尚未绑定，请继续保存租户绑定",
      );
    } catch (e: any) {
      setError(e.message);
      toastErr("保存运行副本失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function runDraft(mode: "validate" | "health" | "preview" | "session") {
    if (code === null) return;
    const max = Number(limit);
    if (!Number.isInteger(max) || max < 1 || max > 1000) {
      return toastErr("最大会话数必须为 1 到 1000");
    }
    setBusy(`draft-${mode}`);
    setDraftResult(null);
    try {
      const metadata = draftMeta
        || source?.available.find((item) => item.file === codeFile)
        || null;
      const payload: Record<string, unknown> = {
        file: codeFile.trim(),
        code,
        mode,
      };
      if (mode === "preview") {
        payload.filters = filterBody(metadata);
        payload.max_sessions = max;
      }
      if (mode === "session") {
        payload.session_id = filters.session_id?.trim() || "";
      }
      const result = await api<DraftResult>("/api/datasource/code/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setDraftMeta(result.metadata);
      setDraftResult(result);
      if (mode === "preview") {
        setRows(result.sessions || []);
        setCounts(null);
      }
      toastOk(
        mode === "validate"
          ? "草稿校验通过"
          : mode === "health"
            ? result.health?.ok ? "草稿连接正常" : "草稿连接未通过"
            : mode === "preview"
              ? `草稿匹配 ${result.count || 0} 个 Session`
              : "草稿转换完成",
      );
    } catch (e: any) {
      setError(e.message);
      toastErr("草稿测试失败", e.message);
    } finally {
      setBusy("");
    }
  }

  async function runSaved(action: "test" | "sessions" | "pull" | "refresh") {
    setBusy(action);
    try {
      if (action === "refresh") {
        if (mayDiscard()) {
          closeDraft();
          await refresh();
        }
      } else if (action === "test") {
        setHealth(await api("/api/datasource/test", { method: "POST" }));
      } else {
        const max = Number(limit);
        if (!Number.isInteger(max) || max < 1 || max > 1000) {
          throw new Error("最大会话数必须为 1 到 1000");
        }
        const body = { ...filterBody(), max_sessions: max };
        const endpoint = action === "sessions"
          ? "/api/datasource/sessions"
          : "/api/datasource/pull";
        const result = await api<any>(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        setRows(action === "sessions" ? result.sessions : result.results);
        setCounts(action === "pull" ? result.counts : null);
      }
    } catch (e: any) {
      setError(e.message);
      toastErr("数据源操作失败", e.message);
    } finally {
      setBusy("");
    }
  }

  if (!isAdmin) return <Empty>仅管理员可管理数据源。</Empty>;
  const bindingDirty = file !== (source?.file || "");
  const savedReady = !!source?.configured && source.enabled && !bindingDirty && !busy;
  const activeMeta = draftMeta
    || source?.available.find((item) => item.file === codeFile)
    || (codeFile === source?.file ? source : null);
  const filterMeta = code !== null ? activeMeta : source;
  const missing = (filterMeta?.required_filters || [])
    .some((key) => !filters[key]?.trim());
  const warning = source?.persistence?.warning || WARNING;
  const savedSchedule = source?.schedule || DEFAULT_SCHEDULE;
  const scheduleStatus = source?.schedule_status;
  const scheduleDirty = JSON.stringify(schedule) !== JSON.stringify(savedSchedule);
  const scheduleAdapterReady = !!source?.configured
    && source.enabled
    && !bindingDirty
    && ["from_timestamp", "to_timestamp"].every(
      (key) => source.supported_filters?.includes(key),
    );
  const scheduleLabel = scheduleStatus?.running
    ? "拉取中"
    : scheduleStatus?.last_status === "succeeded"
      ? "上次成功"
      : scheduleStatus?.last_status === "partial"
        ? "上次部分失败"
        : scheduleStatus?.last_status === "failed"
          ? "上次失败"
          : scheduleStatus?.last_status === "deferred"
            ? "等待重试"
            : savedSchedule.enabled
              ? "等待触发"
              : "未启用";
  const scheduleTone = scheduleStatus?.running
    ? "blue"
    : scheduleStatus?.last_status === "succeeded"
      ? "green"
      : scheduleStatus?.last_status === "failed"
        ? "red"
        : scheduleStatus?.last_status === "partial"
          || scheduleStatus?.last_status === "deferred"
          ? "amber"
          : "gray";

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6 p-5">
      <div className="flex gap-3 border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div>
          <div className="font-semibold">适配器修改暂不做持久化托管</div>
          <div className="mt-1 break-words text-xs leading-relaxed">{warning}</div>
          <div className="mt-1 text-xs">适配器以服务进程权限执行，仅上传可信代码。</div>
        </div>
      </div>

      <section className="space-y-4 border-b border-border pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-base font-semibold">租户数据源</h2>
          <Button
            variant="ghost"
            size="sm"
            title="刷新适配器"
            aria-label="刷新适配器"
            disabled={!!busy}
            onClick={() => runSaved("refresh")}
          >
            <RefreshCw className="size-4" />
          </Button>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-0 flex-1 text-xs font-medium">
            适配器文件
            <select
              aria-label="适配器文件"
              value={file}
              disabled={!!busy}
              onChange={(event) => chooseFile(event.target.value)}
              className="mt-2 h-10 w-full min-w-0 rounded-md border border-border bg-background px-2 text-sm"
            >
              <option value="">未绑定</option>
              {file && !source?.available.some((item) => item.file === file) && (
                <option value={file}>{file}（文件缺失）</option>
              )}
              {(source?.available || []).map((item) => (
                <option
                  key={item.file}
                  value={item.file}
                  disabled={
                    !!item.error
                    || !!item.bound_tenant_id
                      && item.bound_tenant_id !== source?.tenant_id
                  }
                >
                  {item.file}
                  {item.bound_tenant_id && item.bound_tenant_id !== source?.tenant_id
                    ? "（已绑定其他租户）"
                    : ""}
                  {item.error ? "（文件无效）" : ""}
                </option>
              ))}
            </select>
          </label>
          <Button disabled={!bindingDirty || !!busy} onClick={saveBinding}>
            <Save className="size-4" />
            {busy === "binding" ? "保存中" : "保存绑定"}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <Pill tone={source?.configured && source.enabled ? "green" : "gray"}>
            {source?.configured ? source.enabled ? "已启用" : "已停用" : "未配置"}
          </Pill>
          {source?.provider && <span>来源：{source.provider}</span>}
          {source?.host && (
            <span className="break-all">数据源地址：{source.host}</span>
          )}
          {source?.project_id && (
            <span className="break-all">上游项目：{source.project_id}</span>
          )}
          {source?.revision && (
            <span className="font-mono text-xs text-muted-foreground">
              SHA {source.revision.slice(0, 12)}
            </span>
          )}
        </div>
        {source?.error && (
          <p className="break-words text-sm text-muted-foreground">{source.error}</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={!savedReady}
            onClick={() => runSaved("test")}
          >
            <PlugZap className="size-4" />
            {busy === "test" ? "测试中" : "测试已保存连接"}
          </Button>
          <Button variant="outline" disabled={!file || !!busy} onClick={loadCode}>
            <Code2 className="size-4" />
            编辑源码
          </Button>
          <input
            ref={uploadRef}
            aria-label="上传适配器文件"
            type="file"
            accept=".py,text/x-python"
            className="hidden"
            onChange={(event) => uploadDraft(event.target.files?.[0])}
          />
          <Button
            variant="outline"
            disabled={!!busy}
            onClick={() => uploadRef.current?.click()}
          >
            <Upload className="size-4" />
            上传 .py
          </Button>
          <Button variant="outline" disabled={!!busy} onClick={newDraft}>
            <FilePlus2 className="size-4" />
            新建
          </Button>
          {health && (
            <span role="status" className="self-center text-sm">
              {health.ok ? "连接正常" : `连接失败：${health.error || "上游不可用"}`}
            </span>
          )}
        </div>
      </section>

      <section className="space-y-4 border-b border-border pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <CalendarClock className="size-4" />
              定时拉取
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              每天按所选时区拉取上一自然日的 Trace，转换后进入 Session 队列等待分析。
            </p>
          </div>
          <Pill tone={scheduleTone}>{scheduleLabel}</Pill>
        </div>

        <label className="flex w-fit items-center gap-2 text-sm font-medium">
          <input
            aria-label="启用每日定时拉取"
            type="checkbox"
            checked={schedule.enabled}
            disabled={!!busy}
            onChange={(event) => setSchedule((current) => ({
              ...current,
              enabled: event.target.checked,
            }))}
            className="size-4 accent-primary"
          />
          启用每日定时拉取
        </label>

        <div className="grid min-w-0 gap-4 sm:grid-cols-3">
          <label className="min-w-0 text-xs font-medium">
            每日触发时间
            <Input
              aria-label="每日触发时间"
              type="time"
              value={schedule.time}
              disabled={!!busy}
              className="mt-2"
              onChange={(event) => setSchedule((current) => ({
                ...current,
                time: event.target.value,
              }))}
            />
          </label>
          <label className="min-w-0 text-xs font-medium">
            时区
            <select
              aria-label="定时拉取时区"
              value={schedule.timezone}
              disabled={!!busy}
              onChange={(event) => setSchedule((current) => ({
                ...current,
                timezone: event.target.value,
              }))}
              className="mt-2 h-10 w-full min-w-0 rounded-md border border-border bg-background px-2 text-sm"
            >
              {SCHEDULE_TIMEZONES.map((timezone) => (
                <option key={timezone} value={timezone}>{timezone}</option>
              ))}
            </select>
          </label>
          <label className="min-w-0 text-xs font-medium">
            单次最大 Session 数
            <Input
              aria-label="定时拉取最大 Session 数"
              type="number"
              min={1}
              max={1000}
              value={schedule.max_sessions}
              disabled={!!busy}
              className="mt-2"
              onChange={(event) => setSchedule((current) => ({
                ...current,
                max_sessions: Number(event.target.value),
              }))}
            />
          </label>
        </div>

        {schedule.enabled && !scheduleAdapterReady && (
          <p className="text-xs text-amber-800">
            启用前需保存一个可用且支持起止时间过滤的适配器绑定。
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            disabled={
              !scheduleDirty
              || !!busy
              || schedule.enabled && !scheduleAdapterReady
            }
            onClick={saveSchedule}
          >
            <Save className="size-4" />
            {busy === "schedule-save" ? "保存中" : "保存定时配置"}
          </Button>
          <Button
            variant="outline"
            disabled={
              !!busy
              || scheduleDirty
              || !savedSchedule.enabled
              || !scheduleAdapterReady
              || scheduleStatus?.running
            }
            onClick={runScheduleNow}
          >
            <Play className="size-4" />
            {busy === "schedule-run" ? "启动中" : "立即拉取昨日 Trace"}
          </Button>
        </div>

        <div className="grid gap-x-8 gap-y-2 border-t border-border pt-4 text-xs sm:grid-cols-2">
          <div className="flex min-w-0 items-start gap-2">
            <Clock3 className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <span className="text-muted-foreground">下次触发</span>
            <span className="break-words">
              {formatScheduleTime(scheduleStatus?.next_run_at, savedSchedule.timezone)}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">业务时间窗口：</span>
            {scheduleStatus?.last_target_date
              ? `${scheduleStatus.last_target_date} 全天（${savedSchedule.timezone}）`
              : "-"}
          </div>
          <div>
            <span className="text-muted-foreground">系统开始时间：</span>
            {formatScheduleTime(scheduleStatus?.last_started_at, savedSchedule.timezone)}
          </div>
          <div>
            <span className="text-muted-foreground">系统完成时间：</span>
            {formatScheduleTime(scheduleStatus?.last_finished_at, savedSchedule.timezone)}
          </div>
          {scheduleStatus?.last_status && (
            <div>
              <span className="text-muted-foreground">上次结果：</span>
              {scheduleStatus.last_total ?? 0} 个 Session
              {scheduleStatus.last_counts
                ? `，${Object.entries(scheduleStatus.last_counts)
                    .filter(([, value]) => value > 0)
                    .map(([key, value]) => `${key} ${value}`)
                    .join("，") || "无新增"}`
                : ""}
            </div>
          )}
          {scheduleStatus?.last_error && (
            <div className="break-words text-destructive sm:col-span-2">
              上次错误：{scheduleStatus.last_error}
            </div>
          )}
        </div>
      </section>

      {code !== null && (
        <section className="space-y-4 border-b border-border pb-5">
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
            <div className="min-w-0">
              <h2 className="text-base font-semibold">运行副本编辑</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                校验和测试直接使用编辑器草稿，不写磁盘。
              </p>
            </div>
            <div className="flex items-center gap-2">
              {dirty && <Pill tone="amber">未保存</Pill>}
              <Button
                variant="ghost"
                size="sm"
                aria-label="关闭源码编辑"
                title="关闭源码编辑"
                onClick={() => mayDiscard() && closeDraft()}
              >
                <X className="size-4" />
              </Button>
            </div>
          </div>
          <label className="block text-xs font-medium">
            文件名
            <Input
              aria-label="适配器文件名"
              className="mt-2 font-mono"
              value={codeFile}
              disabled={!!busy}
              onChange={(event) => {
                setCodeFile(event.target.value);
                setDirty(true);
                setDraftResult(null);
              }}
            />
          </label>
          <Textarea
            aria-label="适配器源码"
            value={code}
            disabled={!!busy}
            spellCheck={false}
            className="h-[480px] resize-y whitespace-pre font-mono text-xs leading-relaxed"
            onChange={(event) => {
              setCode(event.target.value);
              setDirty(true);
              setDraftMeta(null);
              setDraftResult(null);
            }}
          />
          <div className="flex flex-wrap gap-2">
            <Button disabled={!dirty || !!busy} onClick={saveCode}>
              <Save className="size-4" />
              {busy === "saveCode" ? "保存中" : "保存运行副本"}
            </Button>
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={() => runDraft("validate")}
            >
              <CheckCheck className="size-4" />
              校验草稿
            </Button>
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={() => runDraft("health")}
            >
              <PlugZap className="size-4" />
              测试草稿连接
            </Button>
            <Button
              variant="outline"
              disabled={!!busy || missing}
              onClick={() => runDraft("preview")}
            >
              <Play className="size-4" />
              预览草稿
            </Button>
            {(activeMeta?.supported_filters || []).includes("session_id") && (
              <Button
                variant="outline"
                disabled={!!busy || !filters.session_id?.trim()}
                onClick={() => runDraft("session")}
              >
                <Code2 className="size-4" />
                转换单条
              </Button>
            )}
          </div>
          <div className="text-xs text-amber-800">
            正式发布路径：
            <code className="break-all">
              session_ingestion/adapters/{codeFile || "&lt;tenant&gt;.py"}
            </code>
            。保存运行副本后仍需联系项目 Owner 合入并发布。
          </div>
          {draftResult && (
            <pre
              aria-label="草稿测试结果"
              className="max-h-80 overflow-auto whitespace-pre-wrap break-words border border-border bg-surface-subtle p-4 text-xs"
            >
              {JSON.stringify(draftResult, null, 2)}
            </pre>
          )}
        </section>
      )}

      {error && (
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      )}

      <section className="space-y-4">
        <h2 className="text-base font-semibold">Session 拉取</h2>
        <div className="grid min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {(filterMeta?.supported_filters || []).map((key) => (
            <label className="min-w-0 text-xs font-medium" key={key}>
              {LABELS[key] || key}
              {filterMeta?.required_filters?.includes(key) ? " *" : ""}
              <Input
                aria-label={LABELS[key] || key}
                type={key.endsWith("timestamp") ? "datetime-local" : "text"}
                value={filters[key] || ""}
                disabled={!!busy}
                className="mt-2"
                onChange={(event) => setFilters((current) => ({
                  ...current,
                  [key]: event.target.value,
                }))}
              />
            </label>
          ))}
          <label className="text-xs font-medium">
            最大会话数
            <Input
              aria-label="最大会话数"
              type="number"
              min={1}
              max={1000}
              value={limit}
              disabled={!!busy}
              onChange={(event) => setLimit(event.target.value)}
              className="mt-2"
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={!savedReady || missing}
            onClick={() => runSaved("sessions")}
          >
            <List className="size-4" />
            {busy === "sessions" ? "查询中" : "预览已保存"}
          </Button>
          <Button
            disabled={!savedReady || missing}
            onClick={() => runSaved("pull")}
          >
            <DownloadCloud className="size-4" />
            {busy === "pull" ? "拉取中" : "拉取入库"}
          </Button>
        </div>
        {counts && (
          <div className="flex flex-wrap gap-4 text-xs">
            {Object.entries(counts).map(([key, value]) => (
              <span key={key}>{key}: {value}</span>
            ))}
          </div>
        )}
        {rows && (rows.length ? (
          <div className="overflow-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  {["Session", "标题", "用户", "时间", "状态"].map((label) => (
                    <th key={label} className="border-b border-border p-3">{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.session_id}>
                    <td className="max-w-64 break-all border-b border-border p-3">
                      {row.session_id}
                    </td>
                    <td className="max-w-64 break-words border-b border-border p-3">
                      {row.title || "-"}
                    </td>
                    <td className="border-b border-border p-3">{row.user_id || "-"}</td>
                    <td className="border-b border-border p-3">{row.timestamp || "-"}</td>
                    <td className="max-w-64 break-words border-b border-border p-3">
                      {row.reason || row.status || `${row.trace_count ?? "-"} traces`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>没有匹配的 Session。</Empty>
        ))}
      </section>
    </div>
  );
}
