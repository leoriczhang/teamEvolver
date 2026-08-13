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
import {
  api,
  type LangfuseConfig,
  type LangfuseFilters,
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
  host: string;
  public_key: string;
  secret_key: string;
  max_sessions: string;
  default_environment: string;
  default_user_id: string;
  default_tags: string;
}

const EMPTY_CONFIG: ConfigForm = {
  enabled: false,
  host: "https://cloud.langfuse.com",
  public_key: "",
  secret_key: "",
  max_sessions: "",
  default_environment: "",
  default_user_id: "",
  default_tags: "",
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
      host: cfg.host || "https://cloud.langfuse.com",
      public_key: cfg.public_key || "",
      secret_key: "",
      max_sessions: cfg.max_sessions ? String(cfg.max_sessions) : "",
      default_environment: (cfg.default_environment || []).join(", "),
      default_user_id: cfg.default_user_id || "",
      default_tags: (cfg.default_tags || []).join(", "),
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
        host: cfgForm.host.trim(),
        max_sessions: Number(cfgForm.max_sessions) || undefined,
        default_environment: splitList(cfgForm.default_environment),
        default_user_id: cfgForm.default_user_id.trim(),
        default_tags: splitList(cfgForm.default_tags),
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
      toastOk("Langfuse 配置已保存", saved.enabled ? "集成已启用" : "集成已停用");
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

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      {/* ---- Connection status ---- */}
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard
          label="集成状态"
          value={
            <Pill tone={enabled ? (reachable ? "green" : "amber") : "gray"}>
              {enabled ? (reachable ? "已连接" : "未连通") : "未启用"}
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
            <Pill tone={config?.enabled ? "green" : "gray"}>{config?.enabled ? "已启用" : "已停用"}</Pill>
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
              启用 Langfuse 集成
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
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={saveConfig} disabled={!isAdmin || savingCfg}>
                {savingCfg ? "保存中…" : "保存并启用"}
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
              ? `已启用 · ${config.host}`
              : "尚未启用。点击右上角「编辑」填写 Host 与 public/secret key 后保存即可使用。"}
          </div>
        )}
      </Panel>

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
