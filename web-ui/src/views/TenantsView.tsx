import { useCallback, useEffect, useRef, useState } from "react";
import { Panel, StatCard, Pill, Empty } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createTenant,
  getTenantConfig,
  listTenants,
  rotateTenantToken,
  setActiveTenantId,
  setTenantStatus,
  updateTenantConfig,
  type TenantInfo,
  type TenantsResp,
  type UserProfile,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { ArrowRight, Check, Copy, KeyRound, Plus, RefreshCw, Settings2 } from "lucide-react";

function str(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

// Token reveal box: the plaintext agent token is returned exactly once by
// the backend (only its sha256 is stored server-side), so it must be
// presented prominently with a copy action before it is lost.
function TokenReveal({
  label,
  token,
  onDismiss,
}: {
  label: string;
  token: string;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-[12px] border border-amber-300 bg-amber-50 p-4">
      <div className="text-[13px] font-bold text-amber-900">{label}</div>
      <div className="mt-1 text-[11px] leading-relaxed text-amber-800">
        该 token 仅此一次完整展示，服务端只保留其哈希；请立即复制并妥善保管，用于 Agent 侧
        <span className="mono"> Authorization: Bearer </span> 接入。
      </div>
      <div className="mt-3 flex items-center gap-2">
        <code className="mono min-w-0 flex-1 break-all rounded-lg border border-amber-300/70 bg-white/80 px-3 py-2 text-[12px]">
          {token}
        </code>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(token);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            } catch {
              toastErr("复制失败", "请手动选择复制");
            }
          }}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? "已复制" : "复制"}
        </Button>
      </div>
      <div className="mt-3 text-right">
        <Button size="sm" variant="outline" onClick={onDismiss}>
          我已保存，关闭
        </Button>
      </div>
    </div>
  );
}

// Tenant governance owns lifecycle and quotas. Data-source implementation
// details live on the selected tenant's dedicated ingestion page.
function TenantSettingsDialog({
  tenant,
  onClose,
  onOpenDataSource,
}: {
  tenant: TenantInfo;
  onClose: () => void;
  onOpenDataSource: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [maxConcurrentSessions, setMaxConcurrentSessions] = useState("");
  const [maxEvolvePerDay, setMaxEvolvePerDay] = useState("");
  const [sourceSummary, setSourceSummary] = useState({
    enabled: "继承全局",
    host: "继承全局",
    conversion: "继承全局",
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const data = await getTenantConfig(tenant.tenant_id);
        if (cancelled) return;
        const ov = data.config_overrides || {};
        const concurrent = Number(ov.max_concurrent_sessions);
        const daily = Number(ov.max_evolve_per_day);
        setMaxConcurrentSessions(Number.isFinite(concurrent) && concurrent > 0 ? String(concurrent) : "");
        setMaxEvolvePerDay(Number.isFinite(daily) && daily > 0 ? String(daily) : "");
        setSourceSummary({
          enabled:
            ov.langfuse_enabled === true
              ? "已启用"
              : ov.langfuse_enabled === false
                ? "已停用"
                : "继承全局",
          host: str(ov.langfuse_host) || "继承全局",
          conversion:
            ov.datasource_type === "skillopt"
              ? "兼容模式（导入旧版 converter.py）"
              : ov.datasource_type === "langfuse"
                ? "原生 Langfuse 映射"
                : "继承全局",
        });
      } catch (e: any) {
        toastErr("加载租户配置失败", e.message);
        onClose();
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant.tenant_id]);

  async function handleSave() {
    const parseQuota = (raw: string, label: string): number | null => {
      if (!raw.trim() || Number(raw) === 0) return null;
      const value = Number(raw);
      if (!Number.isInteger(value) || value < 1) {
        throw new Error(`${label}必须是正整数，留空或填 0 表示不限制`);
      }
      return value;
    };
    let overrides: Record<string, number | null>;
    try {
      overrides = {
        max_concurrent_sessions: parseQuota(maxConcurrentSessions, "并发 Session 上限"),
        max_evolve_per_day: parseQuota(maxEvolvePerDay, "每日 Evolution 上限"),
      };
    } catch (e: any) {
      toastErr("配额格式不正确", e.message);
      return;
    }
    setSaving(true);
    try {
      await updateTenantConfig(tenant.tenant_id, overrides);
      toastOk("租户配额已保存", `${tenant.display_name} · 新任务按更新后的配额运行`);
      onClose();
    } catch (e: any) {
      toastErr("保存租户配置失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  function label(text: string) {
    return <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{text}</span>;
  }

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[85vh] max-w-[840px] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            租户设置 · {tenant.display_name}
            <span className="mono ml-2 text-xs font-normal text-muted-foreground">{tenant.tenant_id}</span>
          </DialogTitle>
        </DialogHeader>
        {loading ? (
          <div className="py-8 text-center text-sm text-muted-foreground">加载配置中…</div>
        ) : (
          <div className="space-y-5">
            <section className="border-b border-border pb-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="text-[13px] font-bold">数据源接入</div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Langfuse 凭据、转换模式、Mapper 和 Session 拉取统一在数据源接入页维护。
                  </p>
                </div>
                <Button size="sm" variant="outline" onClick={onOpenDataSource}>
                  打开数据源接入
                  <ArrowRight className="size-3.5" />
                </Button>
              </div>
              <div className="grid gap-3 text-xs sm:grid-cols-3">
                <div>
                  <span className="block text-muted-foreground">拉取状态</span>
                  <span className="mt-1 block font-semibold">{sourceSummary.enabled}</span>
                </div>
                <div>
                  <span className="block text-muted-foreground">Host</span>
                  <span className="mono mt-1 block truncate font-semibold" title={sourceSummary.host}>
                    {sourceSummary.host}
                  </span>
                </div>
                <div>
                  <span className="block text-muted-foreground">转换模式</span>
                  <span className="mt-1 block font-semibold">{sourceSummary.conversion}</span>
                </div>
              </div>
            </section>

            <section>
              <div className="mb-1 text-[13px] font-bold">运行配额</div>
              <p className="mb-3 text-xs text-muted-foreground">留空或填 0 表示不限制。</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block">
                  {label("并发 Session 上限")}
                  <Input
                    type="number"
                    min="0"
                    step="1"
                    value={maxConcurrentSessions}
                    placeholder="不限制"
                    onChange={(e) => setMaxConcurrentSessions(e.target.value)}
                  />
                </label>
                <label className="block">
                  {label("每日 Evolution 上限")}
                  <Input
                    type="number"
                    min="0"
                    step="1"
                    value={maxEvolvePerDay}
                    placeholder="不限制"
                    onChange={(e) => setMaxEvolvePerDay(e.target.value)}
                  />
                </label>
              </div>
            </section>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            取消
          </Button>
          <Button size="sm" onClick={handleSave} disabled={loading || saving}>
            {saving ? "保存中…" : "保存配额"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function TenantsView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [resp, setResp] = useState<TenantsResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [accountId, setAccountId] = useState("");
  const [creating, setCreating] = useState(false);
  const [busyTenantId, setBusyTenantId] = useState("");
  // (tenant_id, label, token) — the latest one-shot token reveal.
  const [reveal, setReveal] = useState<{ tenantId: string; label: string; token: string } | null>(null);
  // Tenant whose config editor is open.
  const [configTenant, setConfigTenant] = useState<TenantInfo | null>(null);
  const loaded = useRef(false);

  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async (notify = false) => {
    setLoading(true);
    setError("");
    try {
      const data = await listTenants();
      setResp(data);
      if (notify) toastOk("租户列表已刷新");
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active || !isAdmin || loaded.current) return;
    loaded.current = true;
    refresh();
  }, [active, isAdmin, refresh]);

  if (!isAdmin) {
    return (
      <Panel title="租户管理" count="需要管理员权限">
        <Empty>仅管理员可管理租户；普通用户所在数据由租户 token 在服务端路由。</Empty>
      </Panel>
    );
  }

  const mode = resp?.mode || "single";
  const tenants = resp?.tenants || [];
  const activeCount = tenants.filter((t) => t.status === "active").length;

  async function handleCreate() {
    const name = displayName.trim();
    if (!name) {
      toastErr("创建失败", "请填写租户名称");
      return;
    }
    setCreating(true);
    try {
      const data = await createTenant(name, accountId.trim());
      setDisplayName("");
      setAccountId("");
      setReveal({
        tenantId: data.tenant.tenant_id,
        label: `租户「${data.tenant.display_name}」的 agent 接入 token`,
        token: data.agent_token,
      });
      toastOk("租户已创建", data.tenant.display_name);
      await refresh();
    } catch (e: any) {
      toastErr("创建失败", e.message);
    } finally {
      setCreating(false);
    }
  }

  async function handleRotate(tenant: TenantInfo) {
    if (
      !window.confirm(
        `确定轮换租户「${tenant.display_name}」的 agent token？旧 token 立即失效，使用它的 Agent 将无法再上报数据。`
      )
    ) {
      return;
    }
    setBusyTenantId(tenant.tenant_id);
    try {
      const data = await rotateTenantToken(tenant.tenant_id);
      setReveal({
        tenantId: tenant.tenant_id,
        label: `租户「${tenant.display_name}」的新 agent 接入 token`,
        token: data.agent_token,
      });
      toastOk("token 已轮换", tenant.display_name);
    } catch (e: any) {
      toastErr("轮换失败", e.message);
    } finally {
      setBusyTenantId("");
    }
  }

  async function handleToggleStatus(tenant: TenantInfo) {
    const next = tenant.status === "active" ? "disabled" : "active";
    if (
      next === "disabled" &&
      !window.confirm(
        `确定禁用租户「${tenant.display_name}」？其 agent token 将立即停止鉴权，调度器也不再为它运行进化周期。`
      )
    ) {
      return;
    }
    setBusyTenantId(tenant.tenant_id);
    try {
      await setTenantStatus(tenant.tenant_id, next);
      toastOk(next === "active" ? "租户已启用" : "租户已禁用", tenant.display_name);
      await refresh();
    } catch (e: any) {
      toastErr("状态变更失败", e.message);
    } finally {
      setBusyTenantId("");
    }
  }

  function openDataSource(tenant: TenantInfo) {
    setActiveTenantId(tenant.tenant_id);
    const url = new URL(window.location.href);
    url.searchParams.set("tenant", tenant.tenant_id);
    url.searchParams.set("view", "langfuse");
    window.location.assign(url.toString());
  }

  return (
    <div className="space-y-4">
      {mode === "single" && (
        <div className="rounded-[14px] border border-border bg-surface p-4 text-[12px] leading-relaxed text-muted-foreground shadow-[var(--shadow-soft)]">
          当前为<b className="text-foreground">单租户兼容模式</b>（<span className="mono">storage_pg</span>
          未启用）：只有 default 一个租户，创建/禁用/轮换等管理操作不可用。开启 PostgreSQL
          后端后此页自动切换为多租户管理。
        </div>
      )}

      {reveal && (
        <TokenReveal
          label={reveal.label}
          token={reveal.token}
          onDismiss={() => setReveal(null)}
        />
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label="运行模式" value={mode === "postgres" ? "多租户 (PG)" : "单租户"} />
        <StatCard label="租户总数" value={tenants.length} />
        <StatCard label="活跃租户" value={activeCount} />
      </div>

      {mode === "postgres" && (
        <Panel
          title="新建项目"
          count="token 仅创建时展示一次"
          extra={
            <Button size="sm" variant="outline" disabled={creating} onClick={handleCreate}>
              <Plus className="size-3.5" />
              创建
            </Button>
          }
        >
          <div className="flex flex-wrap items-center gap-3 px-[14px] py-3">
            <Input aria-label="Account ID" value={accountId} placeholder="Account ID（可选）" onChange={e => setAccountId(e.target.value)} className="max-w-[260px]" />
            <Input
              value={displayName}
              placeholder="项目名称"
              onChange={(e) => setDisplayName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreate();
              }}
              className="max-w-[360px]"
            />
            <span className="text-[11px] text-muted-foreground">
              创建后自动生成 <span className="mono">tevt_</span> 前缀的 agent 接入 token。
            </span>
          </div>
        </Panel>
      )}

      <Panel
        title="租户列表"
        count={`${tenants.length} 个租户`}
        extra={
          <Button size="sm" variant="outline" disabled={loading} onClick={() => refresh(true)}>
            <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
            刷新
          </Button>
        }
      >
        {error ? (
          <Empty>
            加载失败：{error}
            <br />
            （多租户 API 需要 storage_pg 已启用且当前账号为管理员）
          </Empty>
        ) : tenants.length === 0 ? (
          <Empty>暂无租户数据。</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="border-b border-line bg-surface-subtle text-left text-[11px] font-[700] text-muted-foreground">
                  <th className="px-[14px] py-2.5">租户</th>
                  <th className="px-[14px] py-2.5">tenant_id</th>
                  <th className="px-[14px] py-2.5">状态</th>
                  <th className="px-[14px] py-2.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {tenants.map((t) => (
                  <tr key={t.tenant_id} className="border-b border-line/60 last:border-0">
                    <td className="px-[14px] py-2.5 font-semibold">{t.display_name}</td>
                    <td className="mono px-[14px] py-2.5 text-muted-foreground">{t.tenant_id}</td>
                    <td className="px-[14px] py-2.5">
                      <Pill tone={t.status === "active" ? "green" : "gray"}>
                        {t.status === "active" ? "活跃" : "已禁用"}
                      </Pill>
                    </td>
                    <td className="px-[14px] py-2.5 text-right">
                      <div className="flex justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={mode !== "postgres" || t.tenant_id === "default"}
                          onClick={() => setConfigTenant(t)}
                          title={
                            t.tenant_id === "default"
                              ? "default 租户使用全局配置，无租户配额覆盖"
                              : "查看数据源接入摘要并设置租户运行配额"
                          }
                        >
                          <Settings2 className="size-3.5" />
                          设置
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={mode !== "postgres" || busyTenantId === t.tenant_id}
                          onClick={() => handleRotate(t)}
                          title="轮换 agent 接入 token（旧 token 立即失效）"
                        >
                          <KeyRound className="size-3.5" />
                          轮换 token
                        </Button>
                        <Button
                          size="sm"
                          variant={t.status === "active" ? "outline" : "default"}
                          disabled={mode !== "postgres" || busyTenantId === t.tenant_id}
                          onClick={() => handleToggleStatus(t)}
                        >
                          {t.status === "active" ? "禁用" : "启用"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {configTenant && (
        <TenantSettingsDialog
          tenant={configTenant}
          onClose={() => setConfigTenant(null)}
          onOpenDataSource={() => openDataSource(configTenant)}
        />
      )}
    </div>
  );
}
