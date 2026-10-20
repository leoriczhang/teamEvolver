import { api } from "@/api/client";
import { type UserProfile } from "@/features/admin/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { toastErr,toastOk } from "@/lib/toast";
import { useCallback,useEffect,useRef,useState } from "react";
import { AdapterFile,Datasource,DatasourceSchedule,DatasourceScheduleStatus,DEFAULT_SCHEDULE,DraftResult,SessionRow,TEMPLATE,toIsoUtc,WARNING } from "./DataSourcesViewShared";

export function useDataSourcesView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
const uploadRef = useRef<HTMLInputElement>(null);
const manualPullJobRef = useRef("");
const [source, setSource] = useState<Datasource | null>(null);
const [file, setFile] = useState("");
const [filters, setFilters] = useState<Record<string, string>>({});
const [previewLimit, setPreviewLimit] = useState("100");
const [health, setHealth] = useState<{ ok: boolean; error?: string } | null>(null);
const [rows, setRows] = useState<SessionRow[] | null>(null);
const [counts, setCounts] = useState<Record<string, number> | null>(null);
const [code, setCode] = useState<string | null>(null);
const [codeFile, setCodeFile] = useState("");
const [openedFile, setOpenedFile] = useState("");
const [revision, setRevision] = useState<number | null>(null);
const [dirty, setDirty] = useState(false);
const [draftMeta, setDraftMeta] = useState<AdapterFile | null>(null);
const [draftResult, setDraftResult] = useState<Record<string, unknown> | null>(null);
const [schedule, setSchedule] = useState<DatasourceSchedule>(DEFAULT_SCHEDULE);
const [schedulePolling, setSchedulePolling] = useState(false);
const [busy, setBusy] = useState("");
const [error, setError] = useState("");
const [pullSummary, setPullSummary] = useState<{
    total: number;
    shown: number;
    truncated: boolean;
  } | null>(null);
const isAdmin = user?.role === "admin";
const canEditSource = Boolean(source?.source_editable);
const refresh = useCallback(async (selected?: string) => {
    const data = await api<Datasource>("/api/datasource");
    setSource(data);
    setFile(selected ?? data.file);
    setHealth(null);
    setRows(null);
    setCounts(null);
    setPullSummary(null);
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
    const status = data.schedule_status;
    if (
      status.job_type === "manual"
      && (!manualPullJobRef.current || status.job_id === manualPullJobRef.current)
    ) {
      const currentTotal = status.running
        ? Number(status.current_total || 0)
        : Number(status.last_total || 0);
      setCounts(status.running
        ? status.current_counts || {}
        : status.last_counts || {});
      setPullSummary({
        total: currentTotal,
        shown: status.running ? 0 : status.last_results?.length || 0,
        truncated: status.running
          ? false
          : Boolean(status.last_results_truncated),
      });
      if (!status.running) {
        setRows(status.last_results || []);
        manualPullJobRef.current = "";
        const response = await api<{ result: Record<string, number> | null }>(
          `/api/datasource/jobs/${encodeURIComponent(status.job_id || "")}/result`);
        if (response.result) setCounts(response.result);
      }
    }
    if (!status.running) setSchedulePolling(false);
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
        revision: number;
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
        job_id: string;
      }>("/api/datasource/schedule/run", { method: "POST" });
      setSource((current) => current
        ? { ...current, schedule_status: { running: true, job_id: result.job_id, job_type: "scheduled" } }
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
      const saved = await api<{ file: string; revision: number }>(
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
    const max = Number(previewLimit);
    if (!Number.isInteger(max) || max < 1 || max > 1000) {
      return toastErr("预览条数必须为 1 到 1000");
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
      const options = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      };
      const result = mode === "validate"
        ? await api<DraftResult>("/api/datasource/code/test", options)
        : await runSkillCommand<DraftResult>("/api/datasource/code/test", options, "/api/datasource");
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
        const result = await runSkillCommand<{ health: { ok: boolean; error?: string } }>(
          "/api/datasource/test", { method: "POST" }, "/api/datasource");
        setHealth(result.health);
      } else {
        const filter = filterBody();
        const body: Record<string, unknown> = {
          filters: Object.fromEntries(Object.entries(filter).filter(([k]) => !k.endsWith("timestamp"))),
          ...Object.fromEntries(Object.entries(filter).filter(([k]) => k.endsWith("timestamp"))),
        };
        if (action === "sessions") {
          const max = Number(previewLimit);
          if (!Number.isInteger(max) || max < 1 || max > 1000) {
            throw new Error("预览条数必须为 1 到 1000");
          }
          body.max_sessions = max;
          body.filters = filter;
          delete body.from_timestamp;
          delete body.to_timestamp;
        }
        const endpoint = action === "sessions"
          ? "/api/datasource/sessions"
          : "/api/datasource/pull";
        const options = {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        };
        const result = action === "sessions"
          ? await runSkillCommand<any>(endpoint, options, "/api/datasource")
          : await api<any>(endpoint, options);
        if (action === "sessions") {
          setRows(result.sessions);
          setCounts(null);
          setPullSummary(null);
        } else {
          const status: DatasourceScheduleStatus = {
            running: true, job_id: result.job_id, job_type: "manual", last_status: "queued",
          };
          manualPullJobRef.current = status.job_id || "";
          const total = status.running
            ? Number(status.current_total || 0)
            : Number(status.last_total || 0);
          const resultRows = status.running ? [] : status.last_results || [];
          setRows(status.running ? null : resultRows);
          setCounts(status.running
            ? status.current_counts || {}
            : status.last_counts || {});
          setPullSummary({
            total,
            shown: resultRows.length,
            truncated: status.running
              ? false
              : Boolean(status.last_results_truncated),
          });
          setSource((current) => current
            ? { ...current, schedule_status: status }
            : current);
          setSchedulePolling(status.running);
          toastOk("Session 拉取已启动", "将处理完整时间窗口");
        }
      }
    } catch (e: any) {
      setError(e.message);
      toastErr("数据源操作失败", e.message);
    } finally {
      setBusy("");
    }
  }

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
const scheduleTone: "blue" | "red" | "green" | "gray" | "amber" = scheduleStatus?.running
    ? "blue"
    : scheduleStatus?.last_status === "succeeded"
      ? "green"
      : scheduleStatus?.last_status === "failed"
        ? "red"
        : scheduleStatus?.last_status === "partial"
          || scheduleStatus?.last_status === "deferred"
          ? "amber"
          : "gray";
return { uploadRef, source, file, filters, setFilters, previewLimit, setPreviewLimit, health, rows, counts, code, setCode, codeFile, setCodeFile, revision, dirty, setDirty, setDraftMeta, draftResult, setDraftResult, schedule, setSchedule, busy, error, pullSummary, canEditSource, mayDiscard, closeDraft, chooseFile, loadCode, newDraft, uploadDraft, saveBinding, saveSchedule, runScheduleNow, saveCode, runDraft, runSaved, bindingDirty, savedReady, activeMeta, filterMeta, missing, warning, savedSchedule, scheduleStatus, scheduleDirty, scheduleAdapterReady, scheduleLabel, scheduleTone };
}
