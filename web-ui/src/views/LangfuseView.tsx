import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Empty,
  ListViewport,
  Panel,
  Pill,
  StatCard,
  type PillTone,
} from "@/components/common";
import LegacyConverterPanel from "@/components/LegacyConverterPanel";
import { MapperRegistryPanel } from "@/components/MapperRegistryPanel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  api,
  getActiveTenantId,
  updateTenantConfig,
  type DatasourceConfig,
  type LangfuseConfig,
  type LangfuseFilters,
  type LangfuseMapperEntry,
  type LangfusePullResp,
  type LangfuseSessionsResp,
  type LangfuseSessionPreview,
  type LangfuseStatus,
  type LangfuseTestResp,
  type LangfuseTracingConfig,
  type UserProfile,
} from "@/api/client";
import SessionModal from "@/views/dashboard/SessionModal";
import { fmtTime } from "@/lib/format";
import { toastErr, toastOk } from "@/lib/toast";

// Form state mirrors the LangfuseFilters contract, but keeps list-valued
// fields as raw comma-separated strings for a simpler text-input UX.
interface FilterForm {
  environment: string;
  user_id: string;
  tags: string;
  release: string;
  version: string;
  trace_name: string;
  session_id: string;
  from_timestamp: string;
  to_timestamp: string;
  metadata: string; // "k=v, k2=v2"
  max_sessions: string;
}

const EMPTY_FORM: FilterForm = {
  environment: "",
  user_id: "",
  tags: "",
  release: "",
  version: "",
  trace_name: "",
  session_id: "",
  from_timestamp: "",
  to_timestamp: "",
  metadata: "",
  max_sessions: "",
};

// Tenant-scoped inbound connection + default filters.
interface SourceConfigForm {
  enabled: boolean;
  host: string;
  public_key: string;
  secret_key: string;
  max_sessions: string;
  default_environment: string;
  default_user_id: string;
  default_tags: string;
  default_trace_name: string;
  mappers: LangfuseMapperEntry[];
}

const EMPTY_SOURCE_CONFIG: SourceConfigForm = {
  enabled: false,
  host: "https://cloud.langfuse.com",
  public_key: "",
  secret_key: "",
  max_sessions: "",
  default_environment: "",
  default_user_id: "",
  default_tags: "",
  default_trace_name: "",
  mappers: [],
};

interface TracingConfigForm {
  enabled: boolean;
  host: string;
  public_key: string;
  secret_key: string;
  environment: string;
  release: string;
  sample_rate: string;
  capture_content: boolean;
}

const EMPTY_TRACING_CONFIG: TracingConfigForm = {
  enabled: false,
  host: "",
  public_key: "",
  secret_key: "",
  environment: "local",
  release: "",
  sample_rate: "1",
  capture_content: true,
};

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseMetadata(value: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of value.split(",")) {
    const item = part.trim();
    if (!item) continue;
    const eq = item.indexOf("=");
    if (eq <= 0) continue;
    const key = item.slice(0, eq).trim();
    const val = item.slice(eq + 1).trim();
    if (key) out[key] = val;
  }
  return out;
}

function buildFilters(form: FilterForm): LangfuseFilters {
  const filters: LangfuseFilters = {};
  const env = splitList(form.environment);
  const tags = splitList(form.tags);
  const meta = parseMetadata(form.metadata);
  if (env.length) filters.environment = env;
  if (tags.length) filters.tags = tags;
  if (form.user_id.trim()) filters.user_id = form.user_id.trim();
  if (form.release.trim()) filters.release = form.release.trim();
  if (form.version.trim()) filters.version = form.version.trim();
  if (form.trace_name.trim()) filters.trace_name = form.trace_name.trim();
  if (form.session_id.trim()) filters.session_id = form.session_id.trim();
  if (form.from_timestamp.trim()) filters.from_timestamp = form.from_timestamp.trim();
  if (form.to_timestamp.trim()) filters.to_timestamp = form.to_timestamp.trim();
  if (Object.keys(meta).length) filters.metadata = meta;
  const maxN = Number(form.max_sessions);
  if (Number.isFinite(maxN) && maxN > 0) filters.max_sessions = maxN;
  return filters;
}

const STATUS_TONE: Record<string, PillTone> = {
  queued: "green",
  skipped: "gray",
  duplicate: "amber",
  empty: "gray",
  error: "red",
};

export default function LangfuseView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const isAdmin = user?.role === "admin";
  const projectId = isAdmin ? getActiveTenantId() : "";
  const configPrefix = projectId && projectId !== "default" ? `/api/tenants/${encodeURIComponent(projectId)}` : "/api";
  const configPath = `${configPrefix}/langfuse-config`;
  const [status, setStatus] = useState<LangfuseStatus | null>(null);
  const [config, setConfig] = useState<LangfuseConfig | null>(null);
  const [cfgForm, setCfgForm] = useState<SourceConfigForm>(EMPTY_SOURCE_CONFIG);
  const [tracingConfig, setTracingConfig] = useState<LangfuseTracingConfig | null>(null);
  const [tracingForm, setTracingForm] = useState<TracingConfigForm>(EMPTY_TRACING_CONFIG);
  const [showConfig, setShowConfig] = useState(false);
  const [showTracingConfig, setShowTracingConfig] = useState(false);
  const [form, setForm] = useState<FilterForm>(EMPTY_FORM);
  const [sessions, setSessions] = useState<LangfuseSessionPreview[] | null>(null);
  const [pull, setPull] = useState<LangfusePullResp | null>(null);
  const [ingestedSid, setIngestedSid] = useState<string | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [savingCfg, setSavingCfg] = useState(false);
  const [testingCfg, setTestingCfg] = useState(false);
  const [savingTracing, setSavingTracing] = useState(false);
  const [testingTracing, setTestingTracing] = useState(false);
  const [listing, setListing] = useState(false);
  const [pulling, setPulling] = useState(false);
  const loaded = useRef(false);
  const [dsConfig, setDsConfig] = useState<DatasourceConfig | null>(null);
  const [sourceType, setSourceType] = useState("langfuse");
  const [converterCode, setConverterCode] = useState("");
  const [savingSource, setSavingSource] = useState(false);

  const applyConfigToForms = useCallback((cfg: LangfuseConfig) => {
    setCfgForm({
      enabled: !!cfg.enabled,
      host: cfg.host || "https://cloud.langfuse.com",
      public_key: cfg.public_key || "",
      secret_key: "",
      max_sessions: cfg.max_sessions ? String(cfg.max_sessions) : "",
      default_environment: (cfg.default_environment || []).join(", "),
      default_user_id: cfg.default_user_id || "",
      default_tags: (cfg.default_tags || []).join(", "),
      default_trace_name: cfg.default_trace_name || "",
      mappers: cfg.mappers || [],
    });
  }, []);

  const applyTracingConfigToForm = useCallback((cfg: LangfuseTracingConfig) => {
    setTracingForm({
      enabled: !!cfg.enabled,
      host: cfg.host || "",
      public_key: "",
      secret_key: "",
      environment: cfg.environment || "local",
      release: cfg.release || "",
      sample_rate: String(cfg.sample_rate ?? 1),
      capture_content: cfg.capture_content !== false,
    });
  }, []);

  const refresh = useCallback(
    async (prefillFilters: boolean) => {
      setLoadingStatus(true);
      const statusPromise = api<LangfuseStatus>("/langfuse/status").catch((e: any) => {
        toastErr("加载 Langfuse 状态失败", e.message);
        return null;
      });
      const cfgPromise = api<LangfuseConfig>(configPath).catch((e: any) => {
        // 401 means the session expired — the app-level auth gate will
        // redirect to login; don't double-report with a scary toast.
        if (e?.status !== 401) {
          toastErr("加载 Langfuse 配置失败", e.message);
        }
        return null;
      });
      const tracingPromise = api<LangfuseTracingConfig>("/api/langfuse-tracing-config").catch((e: any) => {
        if (e?.status !== 401) {
          toastErr("加载全局链路观测配置失败", e.message);
        }
        return null;
      });
      const dsPromise = api<DatasourceConfig>(`${configPrefix}/datasource-config`).catch(() => null);
      const [statusData, cfgData, tracingData, dsData] = await Promise.all([
        statusPromise,
        cfgPromise,
        tracingPromise,
        dsPromise,
      ]);
      if (statusData) setStatus(statusData);
      if (cfgData) {
        setConfig(cfgData);
        applyConfigToForms(cfgData);
        if (!cfgData.enabled || !cfgData.public_key_present || !cfgData.secret_key_present) {
          setShowConfig(true);
        }
        // Prefill filter form with configured defaults on first load only.
        if (prefillFilters) {
          setForm((f) => ({
            ...f,
            environment: (cfgData.default_environment || []).join(", "),
            user_id: cfgData.default_user_id || "",
            tags: (cfgData.default_tags || []).join(", "),
            release: cfgData.default_release || "",
            version: cfgData.default_version || "",
            trace_name: cfgData.default_trace_name || "",
          }));
        }
      }
      if (tracingData) {
        setTracingConfig(tracingData);
        applyTracingConfigToForm(tracingData);
        if (
          !tracingData.enabled
          || !tracingData.host
          || !tracingData.public_key_present
          || !tracingData.secret_key_present
        ) {
          setShowTracingConfig(true);
        }
      }
      if (dsData) {
        setDsConfig(dsData);
        setSourceType(dsData.type || "langfuse");
        setConverterCode(dsData.legacy_converter_code || "");
      }
      setLoadingStatus(false);
    },
    [applyConfigToForms, applyTracingConfigToForm]
  );

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh(true);
    }
  }, [active, refresh]);

  async function saveConfig() {
    if (!isAdmin) return;
    setSavingCfg(true);
    try {
      const payload: LangfuseConfig = {
        enabled: cfgForm.enabled,
        host: cfgForm.host.trim(),
        max_sessions: Number(cfgForm.max_sessions) || undefined,
        default_environment: splitList(cfgForm.default_environment),
        default_user_id: cfgForm.default_user_id.trim(),
        default_tags: splitList(cfgForm.default_tags),
        default_trace_name: cfgForm.default_trace_name.trim(),
        mappers: cfgForm.mappers,
      };
      // Only send secrets when the operator typed a new value.
      if (cfgForm.public_key.trim()) payload.public_key = cfgForm.public_key.trim();
      if (cfgForm.secret_key.trim()) payload.secret_key = cfgForm.secret_key.trim();
      const saved = await api<LangfuseConfig>(configPath, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setConfig(saved);
      applyConfigToForms(saved);
      toastOk(
        "数据源 Langfuse 配置已保存",
        saved.enabled ? "会话拉取已启用" : "会话拉取已停用"
      );
      // Re-probe connectivity so the status cards reflect the new credentials.
      await refresh(false);
    } catch (e: any) {
      toastErr("保存 Langfuse 配置失败", e.message);
    } finally {
      setSavingCfg(false);
    }
  }

  async function saveTracingConfig() {
    if (!isAdmin) return;
    setSavingTracing(true);
    try {
      const payload: LangfuseTracingConfig = {
        enabled: tracingForm.enabled,
        host: tracingForm.host.trim(),
        environment: tracingForm.environment.trim() || "local",
        release: tracingForm.release.trim(),
        sample_rate: Number(tracingForm.sample_rate),
        capture_content: tracingForm.capture_content,
      };
      if (tracingForm.public_key.trim()) payload.public_key = tracingForm.public_key.trim();
      if (tracingForm.secret_key.trim()) payload.secret_key = tracingForm.secret_key.trim();
      const saved = await api<LangfuseTracingConfig>("/api/langfuse-tracing-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setTracingConfig(saved);
      applyTracingConfigToForm(saved);
      toastOk(
        "全局链路观测配置已保存",
        saved.enabled ? "所有租户的进化链路将统一上报" : "链路观测已停用"
      );
      await refresh(false);
    } catch (e: any) {
      toastErr("保存全局链路观测配置失败", e.message);
    } finally {
      setSavingTracing(false);
    }
  }

  async function testTracingConfig() {
    if (!isAdmin) return;
    setTestingTracing(true);
    try {
      const payload: Record<string, string> = { host: tracingForm.host.trim() };
      if (tracingForm.public_key.trim()) payload.public_key = tracingForm.public_key.trim();
      if (tracingForm.secret_key.trim()) payload.secret_key = tracingForm.secret_key.trim();
      await api<LangfuseTestResp>("/api/langfuse-tracing-config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toastOk("全局链路观测 Langfuse 连通正常");
    } catch (e: any) {
      toastErr("全局链路观测连通性测试失败", e.message);
    } finally {
      setTestingTracing(false);
    }
  }

  async function testConfig() {
    if (!isAdmin) return;
    setTestingCfg(true);
    try {
      const payload: Record<string, string> = { host: cfgForm.host.trim() };
      if (cfgForm.public_key.trim()) payload.public_key = cfgForm.public_key.trim();
      if (cfgForm.secret_key.trim()) payload.secret_key = cfgForm.secret_key.trim();
      const result = await api<LangfuseTestResp>(`${configPath}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toastOk("Langfuse 连通正常", `远端会话数 ${result.total_sessions ?? "-"}`);
    } catch (e: any) {
      toastErr("Langfuse 连通性测试失败", e.message);
    } finally {
      setTestingCfg(false);
    }
  }

  async function saveSourceConfig() {
    if (!isAdmin) return;
    setSavingSource(true);
    try {
      if (projectId && projectId !== "default") {
        await updateTenantConfig(projectId, {
          datasource_type: sourceType,
          datasource_legacy_converter_code: converterCode.trim() || null,
        });
      } else {
        await api<DatasourceConfig>("/api/datasource-config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: sourceType,
            legacy_converter_code: converterCode,
          }),
        });
      }
      toastOk(
        "转换配置已保存",
        sourceType === "skillopt" ? "使用兼容模式（导入旧版 converter.py）" : "使用原生 Langfuse 映射"
      );
      await refresh(false);
    } catch (e: any) {
      toastErr("保存转换配置失败", e.message);
    } finally {
      setSavingSource(false);
    }
  }

  async function listSessions() {
    setListing(true);
    setPull(null);
    try {
      const data = await api<LangfuseSessionsResp>("/langfuse/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildFilters(form)),
      });
      setSessions(data.sessions || []);
      toastOk("已列出会话", `匹配 ${data.count} 个 session`);
    } catch (e: any) {
      toastErr("列出会话失败", e.message);
    } finally {
      setListing(false);
    }
  }

  async function pullSessions() {
    setPulling(true);
    try {
      const data = await api<LangfusePullResp>("/langfuse/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildFilters(form)),
      });
      setPull(data);
      const c = data.counts || {};
      toastOk(
        "拉取完成",
        `queued ${c.queued || 0} · skipped ${c.skipped || 0} · error ${c.error || 0}`
      );
    } catch (e: any) {
      toastErr("拉取会话失败", e.message);
    } finally {
      setPulling(false);
    }
  }

  const enabled = !!status?.enabled;
  const reachable = !!status?.reachable;
  const usable = enabled && reachable;
  const tracingStatus = tracingConfig?.status || status?.tracing;
  const tracingEnabled = !!tracingStatus?.enabled;
  const tracingInitialized = !!tracingStatus?.initialized;

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      {/* ---- Data source and conversion configuration ---- */}
      <Panel
        title="数据来源与转换"
        extra={
          <Pill tone={dsConfig?.source ? "green" : "gray"}>
            {dsConfig?.source || "langfuse"}
          </Pill>
        }
      >
        <div className="space-y-3 px-4 py-3">
          <div className="text-sm">
            <span className="text-muted-foreground">Session 来源：</span>
            <span className="font-semibold">Langfuse</span>
            <span className="ml-4 text-muted-foreground">转换模式：</span>
            <span className="font-semibold">
              {sourceType === "skillopt" ? "兼容模式（导入旧版 converter.py）" : "原生 Langfuse 映射"}
            </span>
          </div>
          {(!projectId || projectId === "default") && <><div className="text-sm">
            <span className="text-muted-foreground">适配器目录：</span>
            <span className="mono text-xs break-all">{dsConfig?.adapters_dir_resolved || "—"}</span>
          </div>
          {dsConfig?.adapter_files && dsConfig.adapter_files.length > 0 && (
            <div className="text-sm">
              <span className="text-muted-foreground">已配置适配器：</span>
              <span>{dsConfig.adapter_files.length} 个</span>
              {dsConfig.adapter_files.slice(0, 5).map((f) => (
                <Pill key={f.agent_id} tone="blue">{f.agent_id}</Pill>
              ))}
              {dsConfig.adapter_files.length > 5 && (
                <span className="text-xs text-muted-soft"> 等 {dsConfig.adapter_files.length} 个</span>
              )}
            </div>
          )}
          <div className="pt-1 text-xs text-muted-foreground">
            每个智能体可在适配器目录中放置 <code className="mono">{"<agent_id>.py"}</code> 文件，定义项目级的过滤、去重、字段抽取和转换逻辑。文件保存后自动热重载，无需重启服务。
          </div></>}
          <LegacyConverterPanel
            previewPath={
              projectId && projectId !== "default"
                ? `/api/tenants/${encodeURIComponent(projectId)}/converter`
                : undefined
            }
            sourceType={sourceType}
            code={converterCode}
            disabled={!isAdmin}
            onSourceType={setSourceType}
            onChange={setConverterCode}
          />
          <div className="flex items-center justify-end">
            <Button size="sm" onClick={saveSourceConfig} disabled={!isAdmin || savingSource}>
              {savingSource ? "保存中…" : "保存转换配置"}
            </Button>
          </div>
        </div>
      </Panel>

      {/* ---- Connection status ---- */}
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard
          label="会话拉取"
          value={
            <Pill tone={enabled ? (reachable ? "green" : "amber") : "gray"}>
              {enabled ? (reachable ? "已连接" : "未连通") : "未启用"}
            </Pill>
          }
        />
        <StatCard
          label="全局链路观测"
          value={
            <Pill tone={tracingEnabled ? (tracingInitialized ? "green" : "amber") : "gray"}>
              {tracingEnabled ? (tracingInitialized ? "已连接" : "待初始化") : "未启用"}
            </Pill>
          }
        />
        <StatCard label="数据源 Host" value={<span className="mono text-xs break-all">{status?.host || "—"}</span>} />
        <StatCard
          label="观测 Host"
          value={<span className="mono text-xs break-all">{tracingStatus?.host || tracingConfig?.host || "—"}</span>}
        />
        <StatCard
          label="数据源凭据"
          value={
            <span className="text-sm">
              {status?.public_key_present ? "public ✓" : "public ✗"} ·{" "}
              {status?.secret_key_present ? "secret ✓" : "secret ✗"}
            </span>
          }
        />
        <StatCard label="远端会话数" value={status?.total_sessions ?? "—"} />
      </div>

      {enabled && !reachable && status?.reason && status.reason !== "langfuse_disabled" && (
        <div className="mb-5 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          无法连接 Langfuse：{status.reason}
        </div>
      )}

      {/* ---- Tenant-scoped inbound Langfuse settings ---- */}
      <Panel
        title="数据源 Langfuse"
        extra={
          <div className="flex items-center gap-2">
            <Pill tone={config?.enabled ? "green" : "gray"}>
              {config?.enabled ? "已启用" : "已停用"}
            </Pill>
            <Button variant="ghost" size="sm" onClick={() => setShowConfig((v) => !v)}>
              {showConfig ? "收起" : "编辑"}
            </Button>
          </div>
        }
      >
        {showConfig && (
          <div className="space-y-4 p-4">
            {!isAdmin && (
              <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
                当前账号不是管理员，只能查看 Langfuse 配置。
              </div>
            )}
            <label className="flex items-center gap-2 text-sm font-semibold">
              <input
                type="checkbox"
                disabled={!isAdmin}
                checked={cfgForm.enabled}
                onChange={(e) => setCfgForm({ ...cfgForm, enabled: e.target.checked })}
              />
              从 Langfuse 拉取会话
            </label>
            <div className="grid gap-3.5 md:grid-cols-2">
              <FormField label="数据源 Host *" hint="当前租户的会话来源">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.host}
                  placeholder="https://cloud.langfuse.com"
                  onChange={(e) => setCfgForm({ ...cfgForm, host: e.target.value })}
                />
              </FormField>
              <FormField label="最大会话数" hint="单次拉取上限，留空用默认 100">
                <Input
                  disabled={!isAdmin}
                  type="number"
                  value={cfgForm.max_sessions}
                  placeholder="100"
                  onChange={(e) => setCfgForm({ ...cfgForm, max_sessions: e.target.value })}
                />
              </FormField>
              <FormField label={`数据源 Public Key *${config?.public_key_present ? "（已配置，可覆盖）" : ""}`}>
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.public_key}
                  placeholder="pk-lf-..."
                  onChange={(e) => setCfgForm({ ...cfgForm, public_key: e.target.value })}
                />
              </FormField>
              <FormField label={`数据源 Secret Key *${config?.secret_key_present ? "（已配置，留空保留）" : ""}`}>
                <Input
                  disabled={!isAdmin}
                  type="password"
                  value={cfgForm.secret_key}
                  placeholder={config?.secret_key_present ? "输入新值可替换" : "sk-lf-..."}
                  onChange={(e) => setCfgForm({ ...cfgForm, secret_key: e.target.value })}
                />
              </FormField>
              <FormField label="默认 Environment（逗号分隔）">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.default_environment}
                  placeholder="production, staging"
                  onChange={(e) => setCfgForm({ ...cfgForm, default_environment: e.target.value })}
                />
              </FormField>
              <FormField label="默认 Tags（逗号分隔）">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.default_tags}
                  placeholder="agent, eval"
                  onChange={(e) => setCfgForm({ ...cfgForm, default_tags: e.target.value })}
                />
              </FormField>
              <FormField label="默认 Trace 名称" hint="拉取会话时按 trace.name 过滤">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.default_trace_name}
                  placeholder="openclaw-turn"
                  onChange={(e) => setCfgForm({ ...cfgForm, default_trace_name: e.target.value })}
                />
              </FormField>
              <FormField label="默认 User ID">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.default_user_id}
                  placeholder="（可选）"
                  onChange={(e) => setCfgForm({ ...cfgForm, default_user_id: e.target.value })}
                />
              </FormField>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={saveConfig} disabled={!isAdmin || savingCfg}>
                {savingCfg ? "保存中…" : "保存配置"}
              </Button>
              <Button variant="outline" size="sm" onClick={testConfig} disabled={!isAdmin || testingCfg}>
                {testingCfg ? "测试中…" : "测试连通性"}
              </Button>
              <span className="ml-auto text-xs text-muted-foreground">
                保存后立即生效，无需重启服务。密钥不会回显。
              </span>
            </div>
          </div>
        )}
        {!showConfig && (
          <div className="px-4 py-3 text-xs text-muted-foreground">
            {config?.enabled
              ? `会话拉取已启用 · ${config.host}`
              : "尚未启用当前租户的数据源连接。"}
          </div>
        )}
      </Panel>

      {/* ---- Service-wide outbound tracing settings ---- */}
      <Panel
        title="全局链路观测 Langfuse"
        count="服务级配置 · 所有租户共用"
        extra={
          <div className="flex items-center gap-2">
            <Pill tone={tracingConfig?.enabled ? "green" : "gray"}>
              {tracingConfig?.enabled ? "已启用" : "已停用"}
            </Pill>
            <Button variant="ghost" size="sm" onClick={() => setShowTracingConfig((v) => !v)}>
              {showTracingConfig ? "收起" : "编辑"}
            </Button>
          </div>
        }
      >
        {showTracingConfig ? (
          <div className="space-y-4 p-4">
            <label className="flex items-center gap-2 text-sm font-semibold">
              <input
                type="checkbox"
                disabled={!isAdmin}
                checked={tracingForm.enabled}
                onChange={(e) => setTracingForm({ ...tracingForm, enabled: e.target.checked })}
              />
              上报进化与团队 Memory 链路
            </label>
            <div className="grid gap-3.5 md:grid-cols-2">
              <FormField label="观测 Host *" hint="独立于各租户的数据源 Langfuse">
                <Input
                  disabled={!isAdmin}
                  value={tracingForm.host}
                  placeholder="https://cloud.langfuse.com"
                  onChange={(e) => setTracingForm({ ...tracingForm, host: e.target.value })}
                />
              </FormField>
              <FormField label="观测 Environment" hint="例如 local、staging、production">
                <Input
                  disabled={!isAdmin}
                  value={tracingForm.environment}
                  placeholder="local"
                  onChange={(e) => setTracingForm({ ...tracingForm, environment: e.target.value })}
                />
              </FormField>
              <FormField label={`观测 Public Key *${tracingConfig?.public_key_present ? "（已配置，可覆盖）" : ""}`}>
                <Input
                  disabled={!isAdmin}
                  value={tracingForm.public_key}
                  placeholder="pk-lf-..."
                  onChange={(e) => setTracingForm({ ...tracingForm, public_key: e.target.value })}
                />
              </FormField>
              <FormField label={`观测 Secret Key *${tracingConfig?.secret_key_present ? "（已配置，留空保留）" : ""}`}>
                <Input
                  disabled={!isAdmin}
                  type="password"
                  value={tracingForm.secret_key}
                  placeholder={tracingConfig?.secret_key_present ? "输入新值可替换" : "sk-lf-..."}
                  onChange={(e) => setTracingForm({ ...tracingForm, secret_key: e.target.value })}
                />
              </FormField>
              <FormField label="观测 Release" hint="可填写版本号或 Git SHA">
                <Input
                  disabled={!isAdmin}
                  value={tracingForm.release}
                  placeholder="（可选）"
                  onChange={(e) => setTracingForm({ ...tracingForm, release: e.target.value })}
                />
              </FormField>
              <FormField label="采样率" hint="0 到 1；本地调试建议 1">
                <Input
                  disabled={!isAdmin}
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  value={tracingForm.sample_rate}
                  onChange={(e) => setTracingForm({ ...tracingForm, sample_rate: e.target.value })}
                />
              </FormField>
              <label className="flex items-center gap-2 self-end pb-2 text-sm">
                <input
                  type="checkbox"
                  disabled={!isAdmin}
                  checked={tracingForm.capture_content}
                  onChange={(e) => setTracingForm({ ...tracingForm, capture_content: e.target.checked })}
                />
                采集模型输入与输出
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={saveTracingConfig} disabled={!isAdmin || savingTracing}>
                {savingTracing ? "保存中…" : "保存全局观测配置"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={testTracingConfig}
                disabled={!isAdmin || testingTracing}
              >
                {testingTracing ? "测试中…" : "测试观测连接"}
              </Button>
              <span className="ml-auto text-xs text-muted-foreground">
                该连接不参与会话拉取，保存后立即对所有租户生效。
              </span>
            </div>
          </div>
        ) : (
          <div className="px-4 py-3 text-xs text-muted-foreground">
            {tracingConfig?.enabled
              ? `全租户统一上报 · ${tracingConfig.host} · Environment ${tracingConfig.environment || "local"}`
              : "全局链路观测未启用。"}
          </div>
        )}
      </Panel>

      {/* ---- White-box: per-agent mapper registry ---- */}
      <MapperRegistryPanel
        isAdmin={isAdmin}
        mappers={cfgForm.mappers}
        onChange={(m) => setCfgForm((f) => ({ ...f, mappers: m }))}
        onSave={saveConfig}
        saving={savingCfg}
      />

      {/* ---- Session-attribute filters ---- */}
      <Panel
        title="会话属性筛选"
        extra={
          <Button variant="ghost" size="sm" onClick={() => refresh(false)} disabled={loadingStatus}>
            刷新状态
          </Button>
        }
      >
        <div className="grid grid-cols-1 gap-3.5 p-4 sm:grid-cols-2 lg:grid-cols-3">
          <FormField label="Environment（逗号分隔）" hint="例如 production, staging">
            <Input
              value={form.environment}
              placeholder="production, staging"
              onChange={(e) => setForm({ ...form, environment: e.target.value })}
            />
          </FormField>
          <FormField label="User ID" hint="按 trace.userId 过滤">
            <Input
              value={form.user_id}
              placeholder="u-123"
              onChange={(e) => setForm({ ...form, user_id: e.target.value })}
            />
          </FormField>
          <FormField label="Tags（逗号分隔，全部匹配）">
            <Input
              value={form.tags}
              placeholder="agent, eval"
              onChange={(e) => setForm({ ...form, tags: e.target.value })}
            />
          </FormField>
          <FormField label="Release">
            <Input
              value={form.release}
              placeholder="v1.2.0"
              onChange={(e) => setForm({ ...form, release: e.target.value })}
            />
          </FormField>
          <FormField label="Version">
            <Input
              value={form.version}
              placeholder="1.0"
              onChange={(e) => setForm({ ...form, version: e.target.value })}
            />
          </FormField>
          <FormField label="Trace 名称">
            <Input
              value={form.trace_name}
              placeholder="agent-run"
              onChange={(e) => setForm({ ...form, trace_name: e.target.value })}
            />
          </FormField>
          <FormField label="Session ID（指定单个）">
            <Input
              value={form.session_id}
              placeholder="session-abc"
              onChange={(e) => setForm({ ...form, session_id: e.target.value })}
            />
          </FormField>
          <FormField label="起始时间（ISO 8601）">
            <Input
              value={form.from_timestamp}
              placeholder="2026-08-01T00:00:00Z"
              onChange={(e) => setForm({ ...form, from_timestamp: e.target.value })}
            />
          </FormField>
          <FormField label="结束时间（ISO 8601）">
            <Input
              value={form.to_timestamp}
              placeholder="2026-08-11T00:00:00Z"
              onChange={(e) => setForm({ ...form, to_timestamp: e.target.value })}
            />
          </FormField>
          <FormField label="Metadata（key=value，逗号分隔）" hint="例如 customer_tier=enterprise">
            <Input
              value={form.metadata}
              placeholder="customer_tier=enterprise, region=cn"
              onChange={(e) => setForm({ ...form, metadata: e.target.value })}
            />
          </FormField>
          <FormField label="最大会话数" hint="0 或空 = 使用配置默认">
            <Input
              value={form.max_sessions}
              placeholder={String(status?.max_sessions ?? 100)}
              onChange={(e) => setForm({ ...form, max_sessions: e.target.value })}
            />
          </FormField>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-line bg-surface-subtle px-4 py-3">
          <Button size="sm" variant="outline" onClick={listSessions} disabled={!usable || listing}>
            {listing ? "列出中…" : "列出会话"}
          </Button>
          <Button size="sm" onClick={pullSessions} disabled={!usable || pulling}>
            {pulling ? "拉取中…" : "拉取入库并触发进化"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setForm(EMPTY_FORM);
              setSessions(null);
              setPull(null);
            }}
          >
            清空条件
          </Button>
          <span className="ml-auto text-xs text-muted-foreground">
            {usable
              ? "仅带 trace 级属性时会自动经 /traces 端点解析 session。"
              : "请先在上方「连接配置」中启用并保存后再拉取。"}
          </span>
        </div>
      </Panel>

      {/* ---- Pull result summary ---- */}
      {pull && (
        <Panel title="拉取结果" count={`共 ${pull.total} 个`}>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(120px,1fr))] gap-3 p-4">
            {(["queued", "skipped", "duplicate", "empty", "error"] as const).map((k) => (
              <StatCard key={k} label={k} value={pull.counts?.[k] ?? 0} />
            ))}
          </div>
          {pull.results?.length ? (
            <ListViewport maxHeight="360px">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    {["会话", "结果", "轮次", "说明"].map((h) => (
                      <th
                        key={h}
                        className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {pull.results.map((r) => (
                    <tr key={r.session_id}>
                      <Td>
                        {r.status === "queued" ? (
                          <span
                            className="link mono text-xs"
                            title="点击查看已入库的会话详情"
                            onClick={() => setIngestedSid(r.session_id)}
                          >
                            {r.session_id}
                          </span>
                        ) : (
                          <span className="mono text-xs">{r.session_id}</span>
                        )}
                      </Td>
                      <Td>
                        <Pill tone={STATUS_TONE[r.status] || "gray"}>{r.status}</Pill>
                      </Td>
                      <Td>{r.turns ?? "—"}</Td>
                      <Td className="text-xs text-muted-foreground">
                        {r.reason || r.value_judge?.reason || "—"}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ListViewport>
          ) : (
            <Empty>没有拉取到会话。</Empty>
          )}
        </Panel>
      )}

      {/* ---- Session preview list ---- */}
      {sessions && (
        <Panel title="匹配的会话" count={`${sessions.length} 个`}>
          {!sessions.length ? (
            <Empty>没有匹配的会话。请调整筛选条件后重试。</Empty>
          ) : (
            <ListViewport maxHeight="520px">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    {["Session", "标题", "用户", "环境", "Traces", "标签", "时间"].map((h) => (
                      <th
                        key={h}
                        className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr key={s.session_id}>
                      <Td>
                        <span className="mono text-xs">{s.session_id}</span>
                      </Td>
                      <Td>
                        <span className="block max-w-[220px] truncate text-xs text-muted-foreground">
                          {s.title || "(无标题)"}
                        </span>
                      </Td>
                      <Td className="text-xs">{s.user_id || "—"}</Td>
                      <Td>{s.environment ? <Pill tone="blue">{s.environment}</Pill> : "—"}</Td>
                      <Td>{s.trace_count ?? "—"}</Td>
                      <Td>
                        <span className="flex flex-wrap gap-1">
                          {(s.tags || []).slice(0, 4).map((t) => (
                            <Pill key={t} tone="purple">
                              {t}
                            </Pill>
                          ))}
                          {!s.tags?.length && "—"}
                        </span>
                      </Td>
                      <Td className="text-xs text-muted-foreground">{fmtTime(s.timestamp)}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ListViewport>
          )}
        </Panel>
      )}
      <SessionModal
        sid={ingestedSid}
        initialTab="detail"
        open={!!ingestedSid}
        onClose={() => setIngestedSid(null)}
      />
    </div>
  );
}

function FormField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted-soft">{hint}</span>}
    </label>
  );
}

function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <td className={`border-b border-line px-4 py-2.5 align-top text-sm ${className}`}>{children}</td>
  );
}
