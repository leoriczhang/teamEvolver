import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Panel,
  StatCard,
  Dot,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  registerAgentIntegration,
    type AgentIntegration,
    type AgentIntegrationsResp,
  type EvolveModelSettings,
  type RegisterAgentPayload,
  type SharingConfig,
  type SkillListResp,
  type StatusResp,
  type StorageStatus,
  type UserProfile,
  type UsersListResp,
  type VikingDeployment,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { RefreshCw } from "lucide-react";

type Check = {
  name: string;
  ok: boolean;
  detail: string;
  action?: string;
};

type SharingUpdate = {
  enabled: boolean;
  deployment: VikingDeployment;
  endpoint_override: string;
  account: string;
  personal_user: string;
  team_user?: string;
  root_prefix?: string;
  personal_api_key?: string;
  service_api_key?: string;
  team_api_key?: string;
};

export default function HealthView({
  active,
  user,
  onSharingConfigChange,
}: {
  active: boolean;
  user?: UserProfile | null;
  onSharingConfigChange?: (config: SharingConfig) => void;
}) {
  const [status, setStatus] = useState<StatusResp | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [model, setModel] = useState<EvolveModelSettings | null>(null);
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [skills, setSkills] = useState<SkillListResp | null>(null);
  const [health, setHealth] = useState<{ status?: string } | null>(null);
  const [queueCount, setQueueCount] = useState<number | null>(null);
  const [candidateCount, setCandidateCount] = useState<number | null>(null);
  const [sharing, setSharing] = useState<SharingConfig | null>(null);
  const [agents, setAgents] = useState<AgentIntegration[]>([]);
  const [agentFormOpen, setAgentFormOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [h, st, sto, mdl, us, sk, sess, cands, shr, integrations] = await Promise.allSettled([
        api<{ status?: string }>("/health"),
        api<StatusResp>("/status"),
        api<StorageStatus>("/storage/status"),
        api<EvolveModelSettings>("/api/evolve-model"),
        api<UsersListResp>("/api/users"),
        api<SkillListResp>("/api/skills"),
        api<{ sessions: any[]; total?: number }>("/sessions?limit=1&offset=0"),
        api<{ candidates: any[]; total?: number }>(
          "/api/validation/candidates?compact=true&limit=1&offset=0"
        ),
        api<SharingConfig>("/api/sharing-config"),
        api<AgentIntegrationsResp>("/api/agent-integrations"),
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
      setAgents(
        integrations.status === "fulfilled"
          ? integrations.value.agents || []
          : agents
      );
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
    agents,
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
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  const checks: Check[] = [
    {
      name: "控制台服务",
      ok: health?.status === "ok",
      detail: health?.status === "ok" ? "52010 服务正常响应" : "无法确认 /health 状态",
    },
    {
      name: "对象存储",
      ok: !!storage?.reachable,
      detail: storage
        ? storage.fallback_active
          ? `内置本地存储（回退）· OpenViking ${storage.deployment === "local" ? "自建" : "火山云"}不可达`
          : storage.effective_backend === "local"
            ? `内置本地存储 · ${storage.mirror_enabled ? `技能镜像到 OpenViking${storage.mirror?.backlog ? `（积压 ${storage.mirror.backlog}）` : ""}` : "OpenViking 仅作技能镜像目标"}`
            : `OpenViking · ${storage.deployment === "local" ? "自建" : "火山云"} · ${storage.reachable ? "可达" : storage.reason === "sharing_disabled" ? "同步已暂停" : "不可达"}`
        : "无法读取 /storage/status",
      action: storage?.fallback_active
        ? `OpenViking 暂不可用，数据正写入本地存储${storage.local_root ? `（${storage.local_root}）` : ""}；恢复后新数据自动回到 OpenViking，停机期间的本地数据不会自动回传`
        : storage?.reachable
          ? undefined
          : storage?.reason === "sharing_disabled"
            ? "存储连通正常；团队技能同步（sharing）已关闭，如需同步在配置中开启 sharing.enabled"
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
      ok: users.length > 0 && users.some((u) => u.role === "admin"),
      detail: `${users.length} 个用户 · ${users.filter((u) => u.role === "admin").length} 个管理员`,
      action: users.some((u) => u.role === "admin") ? undefined : "至少保留 1 个管理员账号",
    },
    {
      name: "团队技能库",
      ok: !!skills,
      detail: skills ? `${skills.skills.length} 个团队技能 · ${skills.sharing_enabled ? "云同步开启" : "云同步关闭"}` : "无法读取技能列表",
    },
    {
      name: "Agent 接入协议",
      ok: agents.some((agent) => agent.compatibility === "compatible"),
      detail: `${agents.length} 个接入 · ${agents.filter((agent) => agent.compatibility === "compatible").length} 个 V1`,
      action: agents.length ? undefined : "先完成 Hermes 或 AgentsHub V1 注册",
    },
  ];

  const okCount = checks.filter((c) => c.ok).length;
  const checksPager = usePagedItems(checks);

  const isAdmin = user?.role === "admin";

  const saveDeployment = useCallback(
    async (settings: SharingUpdate) => {
      const saved = await api<StorageStatus>("/api/sharing-config", {
        method: "POST",
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

      <Panel
        title="Agent 接入"
        count={`${agents.length} 个`}
        extra={
          isAdmin ? (
            <Button size="sm" onClick={() => setAgentFormOpen(true)}>
              注册 Agent
            </Button>
          ) : undefined
        }
      >
        <div className="grid gap-3 p-4 md:grid-cols-2">
          {agents.map((agent) => (
            <div key={agent.agent_id} className="rounded-lg border border-border bg-background/60 p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-bold">
                    {agent.display_name || agent.agent_id}
                  </div>
                  <div className="mono mt-1 text-[11px] text-muted-foreground">
                    {agent.agent_id}
                  </div>
                </div>
                <Dot state={agent.status === "active" ? "on" : "off"} />
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                <Info label="协议" value={agent.protocol_version || "legacy"} />
                <Info label="兼容状态" value={agent.compatibility || "legacy"} />
                <Info
                  label="Workspace Token"
                  value={agent.access_token_configured ? "已配置" : "未配置"}
                  state={agent.access_token_configured ? "on" : "off"}
                />
                <Info
                  label="Replay"
                  value={agent.endpoints?.replay_url ? "HTTP" : agent.runtime_type === "hermes" ? "Local" : "未配置"}
                />
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {(agent.capability_ids || agent.capabilities || []).map((capability) => (
                  <span key={capability} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {capability}
                  </span>
                ))}
              </div>
            </div>
          ))}
          {!agents.length && (
            <div className="col-span-full rounded-lg border border-dashed border-border p-5 text-center text-sm text-muted-foreground">
              暂无已注册 Agent。
            </div>
          )}
        </div>
      </Panel>

      <AgentRegisterDialog
        open={agentFormOpen}
        onOpenChange={setAgentFormOpen}
        onRegistered={refresh}
      />

      <DeploymentPanel
        sharing={sharing}
        storage={storage}
        isAdmin={isAdmin}
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
          <Info label="团队技能同步" value={skills?.sharing_enabled ? "开启" : "关闭"} state={skills?.sharing_enabled ? "on" : "off"} />
          <Info label="当前登录角色" value={user?.role === "admin" ? "管理员" : "一般用户"} state={user?.role === "admin" ? "on" : "off"} />
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
  const [personalUser, setPersonalUser] = useState("");
  const [personalKey, setPersonalKey] = useState("");
  const [teamKey, setTeamKey] = useState("");
  const [personalKeyDirty, setPersonalKeyDirty] = useState(false);
  const [teamKeyDirty, setTeamKeyDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Sync local edit state from the server whenever the loaded config changes,
  // unless the user has an unsaved edit in flight.
  useEffect(() => {
    if (dirty || !sharing) return;
    setDeployment((sharing.deployment as VikingDeployment) || "cloud");
    setOverride(sharing.endpoint_override || "");
    setAccount(sharing.account || "default");
    setPersonalUser(sharing.personal_user || "");
    setPersonalKey("");
    setTeamKey("");
    setPersonalKeyDirty(false);
    setTeamKeyDirty(false);
  }, [sharing, dirty]);

  const cloudEndpoint = sharing?.cloud_endpoint || "";
  const localEndpoint = sharing?.local_endpoint || "http://localhost:1933";
  const effectiveEndpoint =
    override.trim() || (deployment === "local" ? localEndpoint : cloudEndpoint);
  // Team space is one-to-one with its OpenViking account; the binding locks in
  // once a service (root) key is configured, so the account becomes read-only.
  const accountBound = Boolean(
    sharing?.account_bound ??
      (sharing?.service_api_key_present ?? sharing?.team_api_key_present)
  );

  async function handleSave() {
    setSaving(true);
    try {
      await onSave({
        enabled: true,
        deployment,
        endpoint_override: override.trim(),
        account: account.trim() || "default",
        personal_user: personalUser.trim(),
        // team_user / root_prefix are no longer user-configurable: the account
        // is the tenant boundary, so the backend keeps its internal defaults.
        ...(personalKeyDirty ? { personal_api_key: personalKey } : {}),
        ...(teamKeyDirty ? { service_api_key: teamKey, team_api_key: teamKey } : {}),
      });
      setDirty(false);
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSaving(false);
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
            Endpoint 覆盖（自建远程地址填这里，如 http://10.37.243.72:1933；留空使用默认地址）
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
                {storage.reachable
                  ? "可达"
                  : storage.reason === "sharing_disabled"
                    ? "同步已暂停"
                    : "不可达"}
              </>
            ) : null}
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <ConfigInput
              label={`OpenViking Account${accountBound ? "（已绑定，不可更改）" : ""}`}
              value={account}
              disabled={!isAdmin || accountBound}
              onChange={(value) => { setAccount(value); setDirty(true); }}
            />
            {accountBound ? (
              <div className="mt-1 text-[11px] text-muted-soft">
                团队空间已与该 account 一对一绑定；配置服务 API Key 后不可再更改。
              </div>
            ) : null}
          </div>
          <ConfigInput label="默认个人 OpenViking 用户" value={personalUser} disabled={!isAdmin || deployment === "local"} placeholder={deployment === "local" ? "自建模式按用户 ID 自动绑定" : "例如 single_evolve3"} onChange={(value) => { setPersonalUser(value); setDirty(true); }} />
          {deployment === "local" ? null : (
            <ConfigInput
              label={`默认个人 API Key${sharing?.personal_api_key_present ? "（已配置，留空保留）" : ""}`}
              value={personalKey}
              type="password"
              disabled={!isAdmin}
              onChange={(value) => { setPersonalKey(value); setPersonalKeyDirty(true); setDirty(true); }}
            />
          )}
          <ConfigInput
            label={`服务 API Key（root key）${(sharing?.service_api_key_present ?? sharing?.team_api_key_present) ? "（已配置，留空保留）" : ""}`}
            value={teamKey}
            type="password"
            disabled={!isAdmin}
            onChange={(value) => { setTeamKey(value); setTeamKeyDirty(true); setDirty(true); }}
          />
        </div>

        <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
          {deployment === "local"
            ? "自建（Trusted）模式下只需管理员配置一个服务 API Key（OpenViking root key），即可管理团队资源、技能同步与团队记忆聚合；个人用户空间按用户 ID 自动绑定，无需单独的个人 Key。团队空间与 OpenViking account 一对一绑定，配置服务 API Key 后 account 即锁定，不可再更改。"
            : "服务 API Key（即 OpenViking 的 root key）仅管理员可配置，用于写入团队资源、技能同步和团队记忆聚合；普通用户无需也无法设置或持有明文 Key。团队空间与 OpenViking account 一对一绑定，配置服务 API Key 后 account 即锁定，不可再更改。"}
        </div>

        {isAdmin ? (
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={saving || !dirty}
              onClick={() => {
                setDeployment((sharing?.deployment as VikingDeployment) || "cloud");
                setOverride(sharing?.endpoint_override || "");
                setAccount(sharing?.account || "default");
                setPersonalUser(sharing?.personal_user || "");
                setPersonalKey("");
                setTeamKey("");
                setPersonalKeyDirty(false);
                setTeamKeyDirty(false);
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

const EMPTY_AGENT_FORM = {
  agent_id: "",
  runtime_type: "",
  display_name: "",
  runtime_version: "1.0.0",
  replay_url: "",
  orchestration: "server_driven",
  max_interactions: "10",
  auth_profile: "",
  session_ingest: false,
  agent_mode: "plain_http",
  request_template: JSON.stringify(
    { message: "{{prompt}}", session_id: "{{request_id}}", history: "{{history}}" },
    null,
    2,
  ),
  map_final_response: "answer",
  map_messages: "messages",
  map_tool_call_count: "usage.tool_calls",
  map_total_tokens: "usage.total_tokens",
  map_status: "status",
  map_error: "error.message",
};

const AGENT_MODE_HINTS: Record<string, string> = {
  plain_http:
    "Agent 侧零感知：无需安装任何组件，teamEvolver 按下方模板直接调用 Agent 已有接口。",
  server_driven:
    "Agent 侧运行 scripts/replay_turn_server.py 并实现 replay-turn 协议端点。",
};

function AgentRegisterDialog({
  open,
  onOpenChange,
  onRegistered,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRegistered: () => void;
}) {
  const [form, setForm] = useState({ ...EMPTY_AGENT_FORM });
  const [saving, setSaving] = useState(false);
  const [issuedToken, setIssuedToken] = useState("");

  const set = (key: keyof typeof EMPTY_AGENT_FORM) => (value: string | boolean) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const close = () => {
    onOpenChange(false);
    setForm({ ...EMPTY_AGENT_FORM });
    setIssuedToken("");
  };

  const submit = async () => {
    if (!form.agent_id.trim() || !form.runtime_type.trim() || !form.replay_url.trim()) {
      toastErr("请填写 agent_id、runtime_type 和 replay_url");
      return;
    }
    const payload: RegisterAgentPayload = {
      agent_id: form.agent_id.trim(),
      runtime_type: form.runtime_type.trim(),
      runtime_version: form.runtime_version.trim() || "1.0.0",
      display_name: form.display_name.trim() || form.agent_id.trim(),
      replay_url: form.replay_url.trim(),
      orchestration: "server_driven",
      max_interactions: Number(form.max_interactions) || 10,
      auth_profile: form.auth_profile.trim(),
      session_ingest: form.session_ingest,
    };
    if (form.agent_mode === "plain_http") {
      let template: unknown;
      try {
        template = JSON.parse(form.request_template);
      } catch {
        toastErr("请求体模板不是合法 JSON");
        return;
      }
      payload.request_template = template as Record<string, unknown>;
      payload.response_mapping = {
        final_response: form.map_final_response.trim(),
        messages: form.map_messages.trim(),
        tool_call_count: form.map_tool_call_count.trim(),
        total_tokens: form.map_total_tokens.trim(),
        status: form.map_status.trim(),
        "error.message": form.map_error.trim(),
      };
    }
    setSaving(true);
    try {
      const resp = await registerAgentIntegration(payload);
      onRegistered();
      if (resp.credentials?.agent_access_token) {
        setIssuedToken(resp.credentials.agent_access_token);
        toastOk("Agent 注册成功", "请立即保存下方的一次性接入令牌");
      } else {
        toastOk("Agent 注册成功");
        close();
      }
    } catch (e: any) {
      toastErr("Agent 注册失败", e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
    >
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>注册 Agent</DialogTitle>
          <DialogDescription>
            将 Agent 的回放端点接入 teamEvolver。配合
            <span className="mono"> scripts/replay_turn_server.py </span>
            使用时，每个 Agent 注册一条，endpoint 指向
            <span className="mono"> /turn/&lt;runtime_type&gt; </span>路由。
          </DialogDescription>
        </DialogHeader>
        {issuedToken ? (
          <div className="space-y-3 py-2">
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400">
              仅本次显示，关闭后无法再查看，请立即保存。
            </div>
            <Label className="text-xs font-semibold text-muted-foreground">
              Agent 接入令牌（session.ingest.v1）
            </Label>
            <Input readOnly value={issuedToken} className="mono text-xs" onFocus={(e) => e.currentTarget.select()} />
            <DialogFooter>
              <Button onClick={close}>我已保存，关闭</Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="grid gap-3.5 py-1">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  Agent ID *
                </Label>
                <Input
                  value={form.agent_id}
                  placeholder="openclaw:prod"
                  onChange={(e) => set("agent_id")(e.target.value)}
                />
              </div>
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  Runtime Type *
                </Label>
                <Input
                  value={form.runtime_type}
                  placeholder="openclaw"
                  onChange={(e) => set("runtime_type")(e.target.value)}
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  显示名称
                </Label>
                <Input
                  value={form.display_name}
                  placeholder="与 Agent ID 相同"
                  onChange={(e) => set("display_name")(e.target.value)}
                />
              </div>
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  Runtime 版本
                </Label>
                <Input
                  value={form.runtime_version}
                  onChange={(e) => set("runtime_version")(e.target.value)}
                />
              </div>
            </div>
            <div>
              <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                回放端点（replay_url）*
              </Label>
              <Input
                value={form.replay_url}
                placeholder="http://agent-host:8010/turn/openclaw"
                onChange={(e) => set("replay_url")(e.target.value)}
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                teamEvolver 逐轮主动调用该端点（orchestration=server_driven）。
              </p>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  Agent 接口模式
                </Label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-2.5 py-1 text-sm shadow-xs focus-visible:outline-none"
                  value={form.agent_mode}
                  onChange={(e) => set("agent_mode")(e.target.value)}
                >
                  <option value="plain_http">直调已有接口（零感知，推荐）</option>
                  <option value="server_driven">协议端点（需运行脚本）</option>
                </select>
              </div>
              <div>
                <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  最大交互轮数
                </Label>
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={form.max_interactions}
                  onChange={(e) => set("max_interactions")(e.target.value)}
                />
              </div>
            </div>
            <p className="rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground">
              {AGENT_MODE_HINTS[form.agent_mode]}
            </p>
            {form.agent_mode === "plain_http" && (
              <>
                <div>
                  <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                    请求体模板（每轮 POST 的 JSON，支持 {"{{prompt}}"} / {"{{request_id}}"} /{" "}
                    {"{{history}}"} / {"{{skill_content}}"} / {"{{context_snapshot}}"} /{" "}
                    {"{{materials}}"})
                  </Label>
                  <Textarea
                    className="mono min-h-[88px] font-mono text-xs"
                    value={form.request_template}
                    onChange={(e) => set("request_template")(e.target.value)}
                  />
                </div>
                <div>
                  <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                    响应字段映射（Agent 自身响应的点路径）
                  </Label>
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      value={form.map_final_response}
                      placeholder="final_response 路径，如 answer"
                      onChange={(e) => set("map_final_response")(e.target.value)}
                    />
                    <Input
                      value={form.map_messages}
                      placeholder="messages（trace）路径"
                      onChange={(e) => set("map_messages")(e.target.value)}
                    />
                    <Input
                      value={form.map_tool_call_count}
                      placeholder="tool_call_count 路径（缺省按 0）"
                      onChange={(e) => set("map_tool_call_count")(e.target.value)}
                    />
                    <Input
                      value={form.map_total_tokens}
                      placeholder="total_tokens 路径（缺省按 0）"
                      onChange={(e) => set("map_total_tokens")(e.target.value)}
                    />
                    <Input
                      value={form.map_status}
                      placeholder="status 路径（缺省 HTTP 2xx 即成功）"
                      onChange={(e) => set("map_status")(e.target.value)}
                    />
                    <Input
                      value={form.map_error}
                      placeholder="error.message 路径"
                      onChange={(e) => set("map_error")(e.target.value)}
                    />
                  </div>
                </div>
              </>
            )}
            <div>
              <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                回放密钥 Profile（可选）
              </Label>
              <Input
                value={form.auth_profile}
                placeholder="留空 = 回放调用不带 Bearer 密钥（内网可信部署）"
                onChange={(e) => set("auth_profile")(e.target.value)}
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                填写时，teamEvolver 侧需设置
                <span className="mono"> TEAMEVOLVER_AGENT_&lt;PROFILE大写&gt;_REPLAY_API_KEY</span>
                ，并与其一致。
              </p>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={form.session_ingest}
                onChange={(e) => set("session_ingest")(e.target.checked)}
              />
              同时开启会话接入（session.ingest.v1，签发一次性接入令牌）
            </label>
          </div>
        )}
        {!issuedToken && (
          <DialogFooter>
            <Button variant="outline" onClick={close} disabled={saving}>
              取消
            </Button>
            <Button onClick={submit} disabled={saving}>
              {saving ? "注册中…" : "注册"}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
