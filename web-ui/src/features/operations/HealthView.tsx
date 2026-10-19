import { api } from "@/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import {
Dot,
ListViewport,
PaginationControls,
Panel,
StatCard,
usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type UserProfile,type UsersListResp } from "@/features/admin/api/client";
import { type SharingConfig,type StorageStatus,type VikingDeployment,type VikingDirsReport } from "@/features/operations/api/client";
import { type ReplayAdapterInfo } from "@/features/replay/api/client";
import { type EvolveModelSettings,type SkillListResp,type StatusResp } from "@/features/skills/api/client";
import { toastErr,toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { RefreshCw } from "lucide-react";
import { useCallback,useEffect,useRef,useState,type ReactNode } from "react";

type LoggingStatus = {
  path: string | null;
  file_state: string;
  last_write_error?: string | null;
  dropped_count: number;
  file_missed_count?: number;
};

type Check = {
  name: string;
  ok: boolean;
  detail: string;
  action?: string;
};

type SharingUpdate = {
  deployment: VikingDeployment;
  endpoint_override: string;
  account: string;
  team_user?: string;
  root_prefix?: string;
  api_key?: string;
};

export default function HealthView({
  active,
  user,
  onSharingConfigChange,
  activeTenantId,
}: {
  active: boolean;
  user?: UserProfile | null;
  onSharingConfigChange?: (config: SharingConfig) => void;
  activeTenantId?: string;
}) {
  const [status, setStatus] = useState<StatusResp | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [model, setModel] = useState<EvolveModelSettings | null>(null);
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [skills, setSkills] = useState<SkillListResp | null>(null);
  const [health, setHealth] = useState<{ status?: string; ok?: boolean } | null>(null);
  const [queueCount, setQueueCount] = useState<number | null>(null);
  const [candidateCount, setCandidateCount] = useState<number | null>(null);
  const [sharing, setSharing] = useState<SharingConfig | null>(null);
  const [replay, setReplay] = useState<ReplayAdapterInfo | null>(null);
  const [logs, setLogs] = useState<LoggingStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [h, st, sto, mdl, us, sk, sess, cands, shr, replayInfo, logInfo] = await Promise.allSettled([
        api<{ status?: string; ok?: boolean }>("/healthz"),
        api<StatusResp>("/status"),
        api<StorageStatus>("/api/storage/status"),
        api<EvolveModelSettings>("/api/model-settings"),
        api<UsersListResp>("/api/users"),
        api<SkillListResp>("/api/skills"),
        api<{ sessions: any[]; total?: number }>("/api/sessions/page?limit=1&offset=0"),
        api<{ candidates: any[]; total?: number }>(
          "/api/skill-candidates?compact=true&limit=1&offset=0"
        ),
        api<SharingConfig>("/api/sharing-config"),
        api<ReplayAdapterInfo>("/api/replay-adapter"),
        user?.platform_role === "platform_admin" ? api<LoggingStatus>("/api/logging/status") : Promise.resolve(null),
      ]);
      setHealth(h.status === "fulfilled" ? h.value : health);
      setStatus(st.status === "fulfilled" ? st.value : status);
      // Keep the last good payload on per-check failure: under evolution load
      // an individual probe may time out; that must not flip a healthy check
      // to "无法读取" (false alarm).
      setStorage(sto.status === "fulfilled" ? sto.value : storage);
      setModel(mdl.status === "fulfilled" ? mdl.value : model);
      setUsers(us.status === "fulfilled" ? us.value.users || [] : users);
      setSkills(sk.status === "fulfilled" ? sk.value : skills);
      if (shr.status === "fulfilled") {
        setSharing(shr.value);
        onSharingConfigChange?.(shr.value);
      }
      if (replayInfo.status === "fulfilled") setReplay(replayInfo.value);
      setLogs(logInfo.status === "fulfilled" ? logInfo.value : null);
      setQueueCount(
        sess.status === "fulfilled"
          ? Number(sess.value.total ?? (sess.value.sessions || []).length)
          : queueCount
      );
      setCandidateCount(
        cands.status === "fulfilled"
          ? Number(cands.value.total ?? (cands.value.candidates || []).length)
          : candidateCount
      );
    } catch (e: any) {
      toastErr("健康检查失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [
    user?.role,
    user?.platform_role,
    candidateCount,
    health,
    model,
    onSharingConfigChange,
    queueCount,
    sharing,
    skills,
    status,
    storage,
    users,
  ]);

  useEffect(() => {
    if (!active) {
      loaded.current = false;
      return;
    }
    if (!loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, activeTenantId, refresh]);

  // Force a reload when the tenant changes (the view stays mounted).
  useEffect(() => {
    loaded.current = false;
    if (active) {
      loaded.current = true;
      refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTenantId]);

  const skillBackend =
    skills?.skill_backend === "postgres"
      ? "PostgreSQL"
      : skills?.skill_backend === "viking"
        ? "OpenViking"
        : skills?.skill_backend === "local"
          ? "本地对象存储"
          : "未知";
  const skillDelivery = "Agent 拉取";

  const checks: Check[] = [
    ...(user?.platform_role === "platform_admin" ? [{
      name: "服务日志",
      ok: logs?.file_state === "active" || logs?.file_state === "disabled",
      detail: logs ? (logs.file_state === "active"
        ? `${logs.path} · 队列丢弃 ${logs.dropped_count} · 未落盘 ${logs.file_missed_count ?? 0}`
        : logs.file_state === "degraded" ? `已降级为平台日志 · ${logs.last_write_error || "文件不可写"}`
        : logs.file_state === "disabled" ? "文件输出已关闭，请查看平台日志" : "日志组件未初始化")
        : "无法读取日志状态",
      action: "日志配置为服务级；目录、轮转参数修改后重启生效",
    } satisfies Check] : []),
    {
      name: "控制台服务",
      ok: health?.status === "ok" || health?.ok === true,
      detail: (health?.status === "ok" || health?.ok === true) ? "52010 服务正常响应" : "无法确认 /healthz 状态",
    },
    {
      name: "对象存储",
      ok: !!storage?.reachable,
      detail: storage
        ? storage.fallback_active
          ? `内置本地存储（回退）· OpenViking ${storage.deployment === "local" ? "自建" : "火山云"}不可达`
          : storage.effective_backend === "local"
            ? `内置本地存储 · ${storage.mirror_enabled ? `技能镜像到 OpenViking${storage.mirror?.backlog ? `（积压 ${storage.mirror.backlog}）` : ""}` : "OpenViking 仅作技能镜像目标"}`
            : `OpenViking · ${storage.deployment === "local" ? "自建" : "火山云"} · ${storage.reachable ? "可达" : "不可达"}`
        : "无法读取 /storage/status",
      action: storage?.fallback_active
        ? `OpenViking 暂不可用，数据正写入本地存储${storage.local_root ? `（${storage.local_root}）` : ""}；恢复后新数据自动回到 OpenViking，停机期间的本地数据不会自动回传`
        : storage?.reachable
          ? undefined
          : storage?.deployment === "local"
            ? "确认自建 openviking-server 已启动且网络可达（Endpoint 覆盖地址是否正确）"
            : "检查火山云 OpenViking Key 与网络连通",
    },
    {
      name: "进化模型",
      ok: !!model?.model && !!model?.base_url && !!model?.api_key_present,
      detail: model ? `${model.model || "未配置模型"} · ${model.api_key_present ? "Key 已配置" : "Key 未配置"}` : "无法读取模型配置",
      action: user?.role === "admin" ? "到模型配置页补齐模型名、Base URL 和 API Key" : "联系管理员检查模型配置",
    },
    ...(storage?.pg
      ? [
          {
            name: "PostgreSQL 存储池",
            ok: !!storage.pg.reachable,
            detail:
              `${storage.pg.reachable ? "可达" : "不可达"} · 延迟 ${storage.pg.ping_ms ?? "—"}ms · 连接 ${storage.pg.pool_idle ?? 0}/${storage.pg.pool_size ?? 0} 空闲 · 池上限 ${storage.pg.pool_max ?? "—"} · 租户 ${storage.pg.tenant_id || "default"}`,
            action: storage.pg.reachable
              ? undefined
              : "PG 不可达：检查网络与 storage_pg.dsn（注意 .local 域名 mDNS 延迟），期间高频写路径依赖本地回退",
          } satisfies Check,
        ]
      : []),
    {
      name: "用户注册表",
      ok: users.length > 0,
      detail: `${users.length} 个可见用户`,
      action: users.length ? undefined : "请检查当前租户的成员关系",
    },
    {
      name: "团队技能库",
      ok: !!skills,
      detail: skills ? `${skills.skills.length} 个团队技能 · ${skillBackend} · ${skillDelivery}` : "无法读取技能列表",
    },

  ];

  const okCount = checks.filter((c) => c.ok).length;
  const checksPager = usePagedItems(checks);

  const isAdmin = user?.role === "admin";

  const saveDeployment = useCallback(
    async (settings: SharingUpdate) => {
      const saved = await api<StorageStatus>("/api/sharing-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      setStorage(saved);
      toastOk(
        "已保存 OpenViking 设置",
        settings.deployment === "local" ? "本地自建" : "云端"
      );
      await refresh();
    },
    [refresh]
  );

  return (
    <div className="mx-auto max-w-[1120px] px-[22px] py-[22px]">
      <div className="mb-5 flex justify-end">
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
          {loading ? "检查中…" : "刷新"}
        </Button>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5">
        <StatCard label="健康项" value={`${okCount}/${checks.length}`} />
        <StatCard label="运行状态" value={status ? (status.running ? "进化中" : "空闲") : "不可达"} />
        <StatCard label="排队会话" value={queueCount ?? status?.pending_sessions ?? "—"} />
        <StatCard label="待评审候选" value={candidateCount ?? "—"} />
        <StatCard label="注册技能" value={status?.registered_skills ?? "—"} />
      </div>

      <Panel title="健康检查" count={`${checks.length} 项`}>
        <ListViewport>
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {["状态", "检查项", "详情", "建议动作"].map((h) => (
                  <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {checksPager.items.map((c) => (
                <tr key={c.name}>
                  <Td><Dot state={c.ok ? "on" : "err"} /></Td>
                  <Td><span className="font-semibold">{c.name}</span></Td>
                  <Td>{c.detail}</Td>
                  <Td>{c.action || <span className="text-muted-foreground">—</span>}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </ListViewport>
        <PaginationControls {...checksPager} onPageChange={checksPager.setPage} />
      </Panel>

      <ReplayBindingPanel info={replay} isAdmin={isAdmin} onSaved={refresh} />

      <DeploymentPanel
        sharing={sharing}
        storage={storage}
        isAdmin={isAdmin && user?.platform_role === "platform_admin"}
        onSave={saveDeployment}
      />

      <Panel title="关键配置概览">
        <div className="grid gap-3 p-4 md:grid-cols-2">
          <Info
            label="存储后端"
            value={
              storage?.fallback_active
                ? "内置存储 · 回退中"
                : storage?.effective_backend === "local"
                  ? "内置本地存储"
                  : `OpenViking · ${storage?.deployment === "local" ? "自建" : "火山云"}`
            }
            state={storage?.reachable ? "on" : "err"}
          />
          <Info
            label="技能镜像"
            value={
              storage?.mirror_enabled
                ? `开启 → OpenViking${storage.mirror?.backlog ? `（积压 ${storage.mirror.backlog}）` : ""}`
                : "关闭"
            }
            state={storage?.mirror_enabled ? "on" : "off"}
          />
          <Info label="存储命名空间" value={storage?.namespace || "未返回"} />
          <Info label="模型" value={model?.model || "未配置"} state={model?.model ? "on" : "err"} />
          <Info label="模型 Base URL" value={model?.base_url || "未配置"} />
          <Info label="团队 Skill 注册表" value={`${skillBackend} · ${skillDelivery}`} state={skills ? "on" : "err"} />
          <Info label="当前登录角色" value={user?.platform_role === "platform_admin" ? "平台超级管理员" : isAdmin ? "租户管理员" : "普通成员"} state={isAdmin ? "on" : "off"} />
        </div>
      </Panel>
    </div>
  );
}

function Info({ label, value, state }: { label: string; value: ReactNode; state?: "on" | "off" | "err" }) {
  return (
    <div className="rounded-lg border border-border bg-background/60 p-3">
      <div className="mb-1 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
        {state && <Dot state={state} />}
        {label}
      </div>
      <div className="break-all text-sm font-semibold">{value}</div>
    </div>
  );
}

function Td({ children }: { children: ReactNode }) {
  return <td className="border-b border-line px-4 py-2.5 align-top text-sm">{children}</td>;
}

function DeploymentPanel({
  sharing,
  storage,
  isAdmin,
  onSave,
}: {
  sharing: SharingConfig | null;
  storage: StorageStatus | null;
  isAdmin: boolean;
  onSave: (settings: SharingUpdate) => Promise<void>;
}) {
  const [deployment, setDeployment] = useState<VikingDeployment>("cloud");
  const [override, setOverride] = useState("");
  const [account, setAccount] = useState("default");
  const [rootKey, setRootKey] = useState("");
  const [rootKeyDirty, setRootKeyDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(false);
  const [dirsReport, setDirsReport] = useState<VikingDirsReport | null>(null);

  // Sync local edit state from the server whenever the loaded config changes,
  // unless the user has an unsaved edit in flight.
  useEffect(() => {
    if (dirty || !sharing) return;
    setDeployment((sharing.deployment as VikingDeployment) || "cloud");
    setOverride(sharing.endpoint_override || "");
    setAccount(sharing.account || "default");
    setRootKey("");
    setRootKeyDirty(false);
  }, [sharing, dirty]);

  const cloudEndpoint = sharing?.cloud_endpoint || "";
  const localEndpoint = sharing?.local_endpoint || "http://localhost:1933";
  const effectiveEndpoint =
    override.trim() || (deployment === "local" ? localEndpoint : cloudEndpoint);

  async function handleSave() {
    setSaving(true);
    try {
      await onSave({
        deployment,
        endpoint_override: override.trim(),
        account: account.trim() || "default",
        // team_user / root_prefix are no longer user-configurable: the account
        // is the tenant boundary, so the backend keeps its internal defaults.
        ...(rootKeyDirty ? { api_key: rootKey } : {}),
      });
      setDirty(false);
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  // Check the OpenViking account namespace and create any missing directory
  // (shared skeleton + registered users' personal spaces). Idempotent: existing
  // directories are never modified. Mainly used to repair accounts created
  // before the automatic bootstrap existed.
  async function handleBootstrapDirs() {
    setBootstrapping(true);
    try {
      const result = await runSkillCommand<{ openviking_dirs: VikingDirsReport }>(
        "/api/sharing-config/bootstrap-dirs",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account: account.trim() || undefined }),
        }
      );
      const report = result.openviking_dirs || {};
      setDirsReport(report);
      const summary =
        `新建 ${report.created?.length || 0} 个 · 已存在 ${report.existing?.length || 0} 个` +
        (report.errors?.length ? ` · ${report.errors.length} 个失败` : "");
      if (report.action === "failed") {
        toastErr("目录检查失败", report.error || report.errors?.[0]?.error || "OpenViking 不可用");
      } else {
        toastOk("OpenViking 目录已就绪", summary);
      }
    } catch (e: any) {
      toastErr("目录检查失败", e.message);
    } finally {
      setBootstrapping(false);
    }
  }

  const options: Array<{ key: VikingDeployment; title: string; desc: string }> = [
    {
      key: "cloud",
      title: "火山云 OpenViking",
      desc: "使用火山引擎托管的 OpenViking 服务，开箱即用。",
    },
    {
      key: "local",
      title: "自建 OpenViking",
      desc: "连接自建的 openviking-server（本机或远程机器，默认 localhost:1933，可在下方 Endpoint 覆盖中填写远程地址）。",
    },
  ];

  return (
    <Panel title="OpenViking 部署">
      <div className="space-y-4 p-4">
        <div className="grid gap-3 md:grid-cols-2">
          {options.map((opt) => {
            const selected = deployment === opt.key;
            return (
              <button
                key={opt.key}
                type="button"
                disabled={!isAdmin}
                onClick={() => {
                  setDeployment(opt.key);
                  setDirty(true);
                }}
                className={cn(
                  "rounded-lg border p-4 text-left transition",
                  selected ? "border-primary bg-primary/5" : "border-border bg-background/60",
                  isAdmin ? "clickable" : "cursor-default opacity-80"
                )}
              >
                <div className="flex items-center gap-2">
                  <Dot state={selected ? "on" : "off"} />
                  <span className="text-sm font-bold">{opt.title}</span>
                </div>
                <div className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{opt.desc}</div>
              </button>
            );
          })}
        </div>

        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
            Endpoint 覆盖（自建远程地址填这里，如 http://openviking.example.com:1933；留空使用默认地址）
          </div>
          <Input
            value={override}
            disabled={!isAdmin}
            placeholder={deployment === "local" ? localEndpoint : cloudEndpoint}
            onChange={(e) => {
              setOverride(e.target.value);
              setDirty(true);
            }}
          />
          <div className="mt-1.5 text-[11px] text-muted-soft">
            生效地址：<span className="mono break-all">{effectiveEndpoint || "—"}</span>
            {storage ? (
              <>
                {" · "}当前{" "}
                {storage.reachable ? "可达" : "不可达"}
              </>
            ) : null}
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <ConfigInput
            label="OpenViking Account"
            value={account}
            disabled={!isAdmin}
            onChange={(value) => { setAccount(value); setDirty(true); }}
          />
          <ConfigInput
            label={`OpenViking Root Key${sharing?.api_key_present ? "（已配置，留空保留）" : ""}`}
            value={rootKey}
            type="password"
            disabled={!isAdmin}
            onChange={(value) => { setRootKey(value); setRootKeyDirty(true); setDirty(true); }}
          />
        </div>

        <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
          Trusted 模式只配置一个 Root Key。服务端通过 Account + Name 访问个人记忆和团队资源，普通用户不持有个人 Key。
        </div>

        {isAdmin ? (
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={bootstrapping}
              onClick={handleBootstrapDirs}
            >
              {bootstrapping ? "检查中…" : "检查并补齐目录"}
            </Button>
            <Button
              variant="outline"
              disabled={saving || !dirty}
              onClick={() => {
                setDeployment((sharing?.deployment as VikingDeployment) || "cloud");
                setOverride(sharing?.endpoint_override || "");
                setAccount(sharing?.account || "default");
                setRootKey("");
                setRootKeyDirty(false);
                setDirty(false);
              }}
            >
              重置
            </Button>
            <Button disabled={saving || !dirty} onClick={handleSave}>
              {saving ? "保存中…" : "保存部署设置"}
            </Button>
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
            仅管理员可切换 OpenViking 部署（云上 / 本地）。
          </div>
        )}

        {dirsReport ? (
          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
            目录检查（account=<span className="mono break-all">{dirsReport.account_id || "—"}</span>）：
            新建 <span className="font-semibold">{dirsReport.created?.length || 0}</span> 个 ·
            已存在 <span className="font-semibold">{dirsReport.existing?.length || 0}</span> 个
            {dirsReport.errors?.length ? (
              <span className="text-destructive">
                {" "}· {dirsReport.errors.length} 个失败：
                {dirsReport.errors
                  .slice(0, 3)
                  .map((item) => `${item.uri}（${item.error}）`)
                  .join("；")}
                {dirsReport.errors.length > 3 ? " …" : ""}
              </span>
            ) : null}
            {dirsReport.error ? (
              <span className="text-destructive"> · {dirsReport.error}</span>
            ) : null}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

function ConfigInput({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  placeholder?: string;
  type?: "text" | "password";
}) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">{label}</div>
      <Input
        type={type}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function ReplayBindingPanel({ info, isAdmin, onSaved }: {
  info: ReplayAdapterInfo | null; isAdmin: boolean; onSaved: () => Promise<void>;
}) {
  const [file, setFile] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => { setFile(info?.file || ""); }, [info?.tenant_id, info?.file]);
  if (!isAdmin) return null;
  async function save() {
    setSaving(true);
    try {
      await api("/api/replay-adapter", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file }),
      });
      toastOk("已保存 Replay 运行环境");
      await onSaved();
    } catch (error: any) { toastErr("保存失败", error.message); }
    finally { setSaving(false); }
  }
  return (
    <Panel title="Replay 运行环境">
      <div className="space-y-3 p-4">
        <p className="text-xs text-muted-foreground">为当前租户选择部署好的执行适配器。启用 True Replay 时需要配置；Session 和 Context 使用租户凭证与用户 ID 接入。</p>
        <div className="flex items-center gap-3">
          <select aria-label="Replay 适配器" className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-2 text-sm"
            value={file} disabled={!info || saving} onChange={(event) => setFile(event.target.value)}>
            <option value="">未绑定</option>
            {info?.file && !info.available.some((item) => item.file === info.file) && <option value={info.file}>{info.file}（不可用）</option>}
            {(info?.available || []).map((item) => <option key={item.file} value={item.file} disabled={!item.enabled}>{item.label || item.file}</option>)}
          </select>
          <Button disabled={!info || saving || file === info.file} onClick={save}>{saving ? "保存中…" : "保存"}</Button>
        </div>
        <p className="text-xs text-muted-foreground">{!info ? "无法读取运行环境配置" : info.configured && info.enabled ? "已绑定；真实执行结果以 Replay 记录为准。" : "尚未绑定可用的运行环境。"}</p>
      </div>
    </Panel>
  );
}
