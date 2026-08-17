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
import {
  api,
    type AgentIntegration,
    type AgentIntegrationsResp,
  type EvolveModelSettings,
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
  deployment: VikingDeployment;
  endpoint_override: string;
  account: string;
  personal_user: string;
  team_user: string;
  root_prefix: string;
  personal_api_key?: string;
  team_api_key?: string;
};

export default function HealthView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
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
      setHealth(h.status === "fulfilled" ? h.value : null);
      setStatus(st.status === "fulfilled" ? st.value : null);
      setStorage(sto.status === "fulfilled" ? sto.value : null);
      setModel(mdl.status === "fulfilled" ? mdl.value : null);
      setUsers(us.status === "fulfilled" ? us.value.users || [] : []);
      setSkills(sk.status === "fulfilled" ? sk.value : null);
      setSharing(shr.status === "fulfilled" ? shr.value : null);
      setAgents(
        integrations.status === "fulfilled"
          ? integrations.value.agents || []
          : []
      );
      setQueueCount(
        sess.status === "fulfilled"
          ? Number(sess.value.total ?? (sess.value.sessions || []).length)
          : null
      );
      setCandidateCount(
        cands.status === "fulfilled"
          ? Number(cands.value.total ?? (cands.value.candidates || []).length)
          : null
      );
    } catch (e: any) {
      toastErr("健康检查失败", e.message);
    } finally {
      setLoading(false);
    }
  }, []);

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
        ? `OpenViking · ${storage.deployment === "local" ? "本地自建" : "云端"} · ${storage.reachable ? "可达" : "不可达"}`
        : "无法读取 /storage/status",
      action: storage?.reachable
        ? undefined
        : storage?.deployment === "local"
          ? "确认本地 openviking-server 已在 http://localhost:1933 运行"
          : "检查云端 OpenViking Key 与网络连通",
    },
    {
      name: "进化模型",
      ok: !!model?.model && !!model?.base_url && !!model?.api_key_present,
      detail: model ? `${model.model || "未配置模型"} · ${model.api_key_present ? "Key 已配置" : "Key 未配置"}` : "无法读取模型配置",
      action: user?.role === "admin" ? "到模型配置页补齐模型名、Base URL 和 API Key" : "联系管理员检查模型配置",
    },
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

      <Panel title="Agent 接入" count={`${agents.length} 个`}>
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
            value={`OpenViking · ${storage?.deployment === "local" ? "本地自建" : "云端"}`}
            state={storage?.reachable ? "on" : "err"}
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
  const [teamUser, setTeamUser] = useState("default");
  const [rootPrefix, setRootPrefix] = useState("team-skill-evolver");
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
    setTeamUser(sharing.team_user || "default");
    setRootPrefix(sharing.root_prefix || "team-skill-evolver");
    setPersonalKey("");
    setTeamKey("");
    setPersonalKeyDirty(false);
    setTeamKeyDirty(false);
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
        personal_user: personalUser.trim(),
        team_user: teamUser.trim() || "default",
        root_prefix: rootPrefix.trim() || "team-skill-evolver",
        ...(personalKeyDirty ? { personal_api_key: personalKey } : {}),
        ...(teamKeyDirty ? { team_api_key: teamKey } : {}),
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
      title: "云上 OpenViking",
      desc: "使用火山托管的 OpenViking 服务，适合团队共享。",
    },
    {
      key: "local",
      title: "本地 OpenViking",
      desc: "连接本机自建的 openviking-server（默认 http://localhost:1933）。",
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
            Endpoint 覆盖（可选，留空使用所选部署的默认地址）
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
                {" · "}当前 {storage.reachable ? "可达" : "不可达"}
              </>
            ) : null}
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <ConfigInput label="OpenViking Account" value={account} disabled={!isAdmin} onChange={(value) => { setAccount(value); setDirty(true); }} />
          <ConfigInput label="团队资源 Root Prefix" value={rootPrefix} disabled={!isAdmin} onChange={(value) => { setRootPrefix(value); setDirty(true); }} />
          <ConfigInput label="默认个人 OpenViking 用户" value={personalUser} disabled={!isAdmin} placeholder="例如 single_evolve3" onChange={(value) => { setPersonalUser(value); setDirty(true); }} />
          <ConfigInput label="团队 OpenViking 用户" value={teamUser} disabled={!isAdmin} placeholder="例如 team_evolve1" onChange={(value) => { setTeamUser(value); setDirty(true); }} />
          <ConfigInput
            label={`默认个人 API Key${sharing?.personal_api_key_present ? "（已配置，留空保留）" : ""}`}
            value={personalKey}
            type="password"
            disabled={!isAdmin}
            onChange={(value) => { setPersonalKey(value); setPersonalKeyDirty(true); setDirty(true); }}
          />
          <ConfigInput
            label={`团队 API Key${sharing?.team_api_key_present ? "（已配置，留空保留）" : ""}`}
            value={teamKey}
            type="password"
            disabled={!isAdmin}
            onChange={(value) => { setTeamKey(value); setTeamKeyDirty(true); setDirty(true); }}
          />
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
                setTeamUser(sharing?.team_user || "default");
                setRootPrefix(sharing?.root_prefix || "team-skill-evolver");
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
