import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Empty,
  ListViewport,
  Panel,
  Pill,
  StatCard,
  type PillTone,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  type LangfuseConfig,
  type LangfuseFilters,
  type LangfuseMapperFormatSpec,
  type LangfuseMapperTemplateResp,
  type LangfuseMapperTestResp,
  type LangfusePullResp,
  type LangfuseSessionsResp,
  type LangfuseSessionPreview,
  type LangfuseStatus,
  type LangfuseTestResp,
  type UserProfile,
} from "@/api/client";
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

// Editable connection + default-filter settings (persisted server-side).
interface ConfigForm {
  enabled: boolean;
  tracing_enabled: boolean;
  host: string;
  public_key: string;
  secret_key: string;
  max_sessions: string;
  default_environment: string;
  default_user_id: string;
  default_tags: string;
  tracing_environment: string;
  tracing_release: string;
  tracing_sample_rate: string;
  tracing_capture_content: boolean;
  mapper_enabled: boolean;
  mapper_code: string;
}

const EMPTY_CONFIG: ConfigForm = {
  enabled: false,
  tracing_enabled: false,
  host: "https://cloud.langfuse.com",
  public_key: "",
  secret_key: "",
  max_sessions: "",
  default_environment: "",
  default_user_id: "",
  default_tags: "",
  tracing_environment: "local",
  tracing_release: "",
  tracing_sample_rate: "1",
  tracing_capture_content: true,
  mapper_enabled: false,
  mapper_code: "",
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
  const [status, setStatus] = useState<LangfuseStatus | null>(null);
  const [config, setConfig] = useState<LangfuseConfig | null>(null);
  const [cfgForm, setCfgForm] = useState<ConfigForm>(EMPTY_CONFIG);
  const [showConfig, setShowConfig] = useState(false);
  const [form, setForm] = useState<FilterForm>(EMPTY_FORM);
  const [sessions, setSessions] = useState<LangfuseSessionPreview[] | null>(null);
  const [pull, setPull] = useState<LangfusePullResp | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [savingCfg, setSavingCfg] = useState(false);
  const [testingCfg, setTestingCfg] = useState(false);
  const [listing, setListing] = useState(false);
  const [pulling, setPulling] = useState(false);
  const loaded = useRef(false);

  const applyConfigToForms = useCallback((cfg: LangfuseConfig) => {
    setCfgForm({
      enabled: !!cfg.enabled,
      tracing_enabled: !!cfg.tracing_enabled,
      host: cfg.host || "https://cloud.langfuse.com",
      public_key: cfg.public_key || "",
      secret_key: "",
      max_sessions: cfg.max_sessions ? String(cfg.max_sessions) : "",
      default_environment: (cfg.default_environment || []).join(", "),
      default_user_id: cfg.default_user_id || "",
      default_tags: (cfg.default_tags || []).join(", "),
      tracing_environment: cfg.tracing_environment || "local",
      tracing_release: cfg.tracing_release || "",
      tracing_sample_rate: String(cfg.tracing_sample_rate ?? 1),
      tracing_capture_content: cfg.tracing_capture_content !== false,
      mapper_enabled: !!cfg.mapper_enabled,
      mapper_code: cfg.mapper_code || "",
    });
  }, []);

  const refresh = useCallback(
    async (prefillFilters: boolean) => {
      setLoadingStatus(true);
      try {
        const [statusData, cfgData] = await Promise.all([
          api<LangfuseStatus>("/langfuse/status"),
          api<LangfuseConfig>("/api/langfuse-config"),
        ]);
        setStatus(statusData);
        setConfig(cfgData);
        applyConfigToForms(cfgData);
        // Auto-open the settings panel when the integration is not yet usable.
        if (
          (!cfgData.enabled && !cfgData.tracing_enabled)
          || !cfgData.public_key_present
          || !cfgData.secret_key_present
        ) {
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
      } catch (e: any) {
        toastErr("加载 Langfuse 状态失败", e.message);
      } finally {
        setLoadingStatus(false);
      }
    },
    [applyConfigToForms]
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
        tracing_enabled: cfgForm.tracing_enabled,
        host: cfgForm.host.trim(),
        max_sessions: Number(cfgForm.max_sessions) || undefined,
        default_environment: splitList(cfgForm.default_environment),
        default_user_id: cfgForm.default_user_id.trim(),
        default_tags: splitList(cfgForm.default_tags),
        tracing_environment: cfgForm.tracing_environment.trim() || "local",
        tracing_release: cfgForm.tracing_release.trim(),
        tracing_sample_rate: Number(cfgForm.tracing_sample_rate),
        tracing_capture_content: cfgForm.tracing_capture_content,
        mapper_enabled: cfgForm.mapper_enabled,
        mapper_code: cfgForm.mapper_code,
      };
      // Only send secrets when the operator typed a new value.
      if (cfgForm.public_key.trim()) payload.public_key = cfgForm.public_key.trim();
      if (cfgForm.secret_key.trim()) payload.secret_key = cfgForm.secret_key.trim();
      const saved = await api<LangfuseConfig>("/api/langfuse-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setConfig(saved);
      applyConfigToForms(saved);
      toastOk(
        "Langfuse 配置已保存",
        saved.enabled || saved.tracing_enabled ? "集成已启用" : "集成已停用"
      );
      // Re-probe connectivity so the status cards reflect the new credentials.
      await refresh(false);
    } catch (e: any) {
      toastErr("保存 Langfuse 配置失败", e.message);
    } finally {
      setSavingCfg(false);
    }
  }

  async function testConfig() {
    if (!isAdmin) return;
    setTestingCfg(true);
    try {
      const payload: Record<string, string> = { host: cfgForm.host.trim() };
      if (cfgForm.public_key.trim()) payload.public_key = cfgForm.public_key.trim();
      if (cfgForm.secret_key.trim()) payload.secret_key = cfgForm.secret_key.trim();
      const result = await api<LangfuseTestResp>("/api/langfuse-config/test", {
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
  const tracingEnabled = !!status?.tracing?.enabled;

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
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
          label="链路观测"
          value={
            <Pill tone={tracingEnabled ? "green" : "gray"}>
              {tracingEnabled ? "已启用" : "未启用"}
            </Pill>
          }
        />
        <StatCard label="Host" value={<span className="mono text-xs break-all">{status?.host || "—"}</span>} />
        <StatCard
          label="凭据"
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

      {/* ---- Editable connection settings (in-console, hot-reload) ---- */}
      <Panel
        title="连接配置"
        extra={
          <div className="flex items-center gap-2">
            <Pill tone={config?.enabled || config?.tracing_enabled ? "green" : "gray"}>
              {config?.enabled || config?.tracing_enabled ? "已启用" : "已停用"}
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
            <label className="flex items-center gap-2 text-sm font-semibold">
              <input
                type="checkbox"
                disabled={!isAdmin}
                checked={cfgForm.tracing_enabled}
                onChange={(e) => setCfgForm({ ...cfgForm, tracing_enabled: e.target.checked })}
              />
              上报进化与团队 Memory 链路
            </label>
            <div className="grid gap-3.5 md:grid-cols-2">
              <FormField label="Host *" hint="Langfuse 部署地址，自部署填自己的">
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
              <FormField
                label={`Public Key *${config?.public_key_present ? "（已配置，可覆盖）" : ""}`}
              >
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.public_key}
                  placeholder="pk-lf-..."
                  onChange={(e) => setCfgForm({ ...cfgForm, public_key: e.target.value })}
                />
              </FormField>
              <FormField
                label={`Secret Key *${config?.secret_key_present ? "（已配置，留空保留）" : ""}`}
              >
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
              <FormField label="默认 User ID">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.default_user_id}
                  placeholder="（可选）"
                  onChange={(e) => setCfgForm({ ...cfgForm, default_user_id: e.target.value })}
                />
              </FormField>
              <FormField label="观测 Environment" hint="例如 local、staging、production">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.tracing_environment}
                  placeholder="local"
                  onChange={(e) => setCfgForm({ ...cfgForm, tracing_environment: e.target.value })}
                />
              </FormField>
              <FormField label="观测 Release" hint="可填写版本号或 Git SHA">
                <Input
                  disabled={!isAdmin}
                  value={cfgForm.tracing_release}
                  placeholder="（可选）"
                  onChange={(e) => setCfgForm({ ...cfgForm, tracing_release: e.target.value })}
                />
              </FormField>
              <FormField label="采样率" hint="0 到 1；本地调试建议 1">
                <Input
                  disabled={!isAdmin}
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  value={cfgForm.tracing_sample_rate}
                  onChange={(e) => setCfgForm({ ...cfgForm, tracing_sample_rate: e.target.value })}
                />
              </FormField>
              <label className="flex items-center gap-2 self-end pb-2 text-sm">
                <input
                  type="checkbox"
                  disabled={!isAdmin}
                  checked={cfgForm.tracing_capture_content}
                  onChange={(e) => setCfgForm({ ...cfgForm, tracing_capture_content: e.target.checked })}
                />
                采集模型输入与输出
              </label>
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
            {config?.enabled || config?.tracing_enabled
              ? `已启用 · ${config.host} · 拉取 ${config.enabled ? "开" : "关"} · 观测 ${config.tracing_enabled ? "开" : "关"}`
              : "尚未启用。点击右上角「编辑」填写 Host 与 public/secret key 后保存即可使用。"}
          </div>
        )}
      </Panel>

      {/* ---- White-box: user-authored trace mapper ---- */}
      <TraceMapperPanel
        isAdmin={isAdmin}
        enabled={cfgForm.mapper_enabled}
        code={cfgForm.mapper_code}
        onToggle={(v) => setCfgForm((f) => ({ ...f, mapper_enabled: v }))}
        onCodeChange={(v) => setCfgForm((f) => ({ ...f, mapper_code: v }))}
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
                        <span className="mono text-xs">{r.session_id}</span>
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

// White-box editor + dry-run tester for the operator-authored trace mapper.
// The mapper is a Python function `map_trace(trace, observations)` that returns
// a partial teamEvolver evolution turn; the server deep-merges it over the
// built-in mapping. Editing/testing is admin-only (it is executable config).
function TraceMapperPanel({
  isAdmin,
  enabled,
  code,
  onToggle,
  onCodeChange,
  onSave,
  saving,
}: {
  isAdmin: boolean;
  enabled: boolean;
  code: string;
  onToggle: (value: boolean) => void;
  onCodeChange: (value: string) => void;
  onSave: () => void | Promise<void>;
  saving: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [traceJson, setTraceJson] = useState("");
  const [testing, setTesting] = useState(false);
  const [loadingTpl, setLoadingTpl] = useState(false);
  const [result, setResult] = useState<LangfuseMapperTestResp | null>(null);
  const [spec, setSpec] = useState<LangfuseMapperFormatSpec | null>(null);
  const [specOpen, setSpecOpen] = useState(false);
  const [loadingSpec, setLoadingSpec] = useState(false);

  // The mapper endpoints were added to the evolve service; when the console
  // talks to an older running service the routes 404. Surface a clear hint to
  // restart rather than a bare "加载失败".
  function describeMapperError(e: any): string {
    const msg = String(e?.message || e || "");
    if (/404|not found|Method Not Allowed|405/i.test(msg)) {
      return "该接口不存在，通常是服务未重启。请重启 teamEvolver 服务后重试。";
    }
    return msg;
  }

  async function fetchTemplate(): Promise<LangfuseMapperTemplateResp> {
    const tpl = await api<LangfuseMapperTemplateResp>("/langfuse/mapper/template");
    if (tpl.spec) setSpec(tpl.spec);
    return tpl;
  }

  async function insertTemplate() {
    setLoadingTpl(true);
    try {
      const tpl = await fetchTemplate();
      if (!code.trim() || window.confirm("用参考模板覆盖当前代码？")) {
        onCodeChange(tpl.template);
      }
      if (!traceJson.trim()) {
        setTraceJson(JSON.stringify(tpl.sample, null, 2));
      }
    } catch (e: any) {
      toastErr("加载模板失败", describeMapperError(e));
    } finally {
      setLoadingTpl(false);
    }
  }

  async function showSpec() {
    setSpecOpen(true);
    if (spec) return;
    setLoadingSpec(true);
    try {
      const tpl = await fetchTemplate();
      if (!tpl.spec) {
        toastErr("加载标准格式说明失败", "服务未返回格式说明，请重启服务后重试。");
      }
    } catch (e: any) {
      toastErr("加载标准格式说明失败", describeMapperError(e));
    } finally {
      setLoadingSpec(false);
    }
  }

  async function runTest() {
    if (!code.trim()) {
      toastErr("无法测试", "请先填写 map_trace 代码");
      return;
    }
    let traceArg: unknown = undefined;
    const raw = traceJson.trim();
    if (raw) {
      try {
        traceArg = JSON.parse(raw);
      } catch (e: any) {
        toastErr("样例 JSON 无法解析", e.message);
        return;
      }
    }
    setTesting(true);
    setResult(null);
    try {
      const data = await api<LangfuseMapperTestResp>("/langfuse/mapper/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, trace: traceArg }),
      });
      setResult(data);
      if (data.ok) {
        toastOk("映射成功", data.used_sample ? "使用内置样例 trace" : "使用自定义 trace");
      } else {
        toastErr("映射失败", data.error || "未知错误");
      }
    } catch (e: any) {
      toastErr("测试请求失败", e.message);
    } finally {
      setTesting(false);
    }
  }

  return (
    <>
    <Panel
      title="自定义 Trace 映射（进化标准格式）"
      extra={
        <div className="flex items-center gap-2">
          <Pill tone={enabled ? "green" : "gray"}>{enabled ? "已启用" : "未启用"}</Pill>
          <Button variant="outline" size="sm" onClick={showSpec} disabled={loadingSpec}>
            {loadingSpec ? "加载中…" : "标准格式说明"}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? "收起" : "编辑"}
          </Button>
        </div>
      }
    >
      {!open && (
        <div className="px-4 py-3 text-xs text-muted-foreground">
          {enabled
            ? "已启用自定义映射：拉取时对每个 trace 调用 map_trace(trace, observations)，结果深合并到内置映射之上。"
            : "未启用。拉取会话时使用内置的 Langfuse → 进化格式映射。点击「编辑」可自定义。"}
        </div>
      )}
      {open && (
        <div className="space-y-4 p-4">
          {!isAdmin && (
            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
              当前账号不是管理员，只能查看映射代码，无法保存或测试。
            </div>
          )}
          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
            编写 <code className="mono">map_trace(trace, observations)</code>，返回一个（可以是部分的）进化标准
            turn 字典；未返回的字段会回退到内置映射，返回 <code className="mono">None</code> 表示完全使用内置映射。
            可用 <code className="mono">json / re / math / datetime</code>，出于安全考虑禁用了{" "}
            <code className="mono">import</code> 与文件访问。
          </div>
          <label className="flex items-center gap-2 text-sm font-semibold">
            <input
              type="checkbox"
              disabled={!isAdmin}
              checked={enabled}
              onChange={(e) => onToggle(e.target.checked)}
            />
            拉取时启用自定义 trace 映射
          </label>
          <FormField label="map_trace 代码（Python）">
            <Textarea
              disabled={!isAdmin}
              value={code}
              spellCheck={false}
              onChange={(e) => onCodeChange(e.target.value)}
              placeholder={"def map_trace(trace, observations):\n    return {\"prompt_text\": str(trace.get(\"input\") or \"\")}"}
              className="mono h-64 text-xs"
            />
          </FormField>
          <FormField
            label="测试用 trace（JSON，可留空使用内置样例）"
            hint="支持 {trace, observations} 或直接是内嵌 observations 的 trace 对象。"
          >
            <Textarea
              disabled={!isAdmin}
              value={traceJson}
              spellCheck={false}
              onChange={(e) => setTraceJson(e.target.value)}
              placeholder='{"trace": {...}, "observations": [...]}'
              className="mono h-40 text-xs"
            />
          </FormField>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={onSave} disabled={!isAdmin || saving}>
              {saving ? "保存中…" : "保存映射"}
            </Button>
            <Button variant="outline" size="sm" onClick={runTest} disabled={!isAdmin || testing}>
              {testing ? "映射中…" : "试运行映射"}
            </Button>
            <Button variant="ghost" size="sm" onClick={insertTemplate} disabled={!isAdmin || loadingTpl}>
              {loadingTpl ? "加载中…" : "插入参考模板"}
            </Button>
            <span className="ml-auto text-xs text-muted-foreground">
              保存后立即生效，无需重启服务。启用前会校验代码可编译。
            </span>
          </div>
          {result && (
            <div className="space-y-2">
              {result.ok ? (
                <>
                  <div className="text-xs text-muted-foreground">
                    映射成功 · {result.used_sample ? "内置样例" : "自定义 trace"} · observations:{" "}
                    {result.observation_count ?? "—"}
                  </div>
                  <div className="grid gap-3 lg:grid-cols-2">
                    <MapperResultBlock title="映射结果（标准格式 turn）" value={result.turn} />
                    <MapperResultBlock title="内置映射（对照）" value={result.builtin} />
                  </div>
                </>
              ) : (
                <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 whitespace-pre-wrap">
                  {result.error || "映射失败"}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Panel>
    <StandardFormatDialog open={specOpen} onOpenChange={setSpecOpen} spec={spec} loading={loadingSpec} />
    </>
  );
}

// Read-only dialog documenting the standard evolution turn format that a mapper
// must produce. Content comes from GET /langfuse/mapper/template's `spec`, so it
// stays in lockstep with the server-side ingest contract.
function StandardFormatDialog({
  open,
  onOpenChange,
  spec,
  loading,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  spec: LangfuseMapperFormatSpec | null;
  loading: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[860px]">
        <DialogHeader>
          <DialogTitle>{spec?.title || "进化标准格式（Evolution Turn）"}</DialogTitle>
          {spec?.summary && <DialogDescription>{spec.summary}</DialogDescription>}
        </DialogHeader>
        {loading && <div className="py-6 text-center text-sm text-muted-foreground">加载中…</div>}
        {!loading && !spec && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            无法加载格式说明。若服务为旧版本，请重启 teamEvolver 服务后重试。
          </div>
        )}
        {!loading && spec && (
          <div className="space-y-4">
            <ListViewport maxHeight="340px">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    {["字段", "类型", "必填", "说明"].map((h) => (
                      <th
                        key={h}
                        className="sticky top-0 border-b border-line bg-surface-subtle px-3 py-2 text-left text-xs font-semibold text-muted-foreground"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {spec.fields.map((f) => (
                    <tr key={f.key}>
                      <td className="border-b border-line px-3 py-2 align-top">
                        <code className="mono text-xs">{f.key}</code>
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs text-muted-foreground">
                        {f.type}
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs">
                        {f.required === true ? (
                          <Pill tone="red">必填</Pill>
                        ) : f.required ? (
                          <Pill tone="amber">{String(f.required)}</Pill>
                        ) : (
                          <span className="text-muted-soft">可选</span>
                        )}
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs text-muted-foreground">
                        {f.desc}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ListViewport>
            <div>
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">示例（一个 turn）</div>
              <ListViewport maxHeight="280px">
                <pre className="mono whitespace-pre-wrap break-all p-3 text-[11px] leading-relaxed">
                  {JSON.stringify(spec.example, null, 2)}
                </pre>
              </ListViewport>
            </div>
          </div>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline" size="sm">
              关闭
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MapperResultBlock({
  title,
  value,
}: {
  title: string;
  value?: Record<string, unknown>;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-subtle">
      <div className="border-b border-line px-3 py-2 text-xs font-semibold text-muted-foreground">
        {title}
      </div>
      <ListViewport maxHeight="320px">
        <pre className="mono whitespace-pre-wrap break-all p-3 text-[11px] leading-relaxed">
          {JSON.stringify(value ?? {}, null, 2)}
        </pre>
      </ListViewport>
    </div>
  );
}
