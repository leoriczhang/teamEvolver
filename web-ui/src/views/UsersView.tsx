import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Panel,
  StatCard,
  Pill,
  Dot,
  Empty,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  api,
  type AgentIntegration,
  type AgentIntegrationsResp,
  type SkillSpaceConfig,
  type TeamSettings,
  type UserProfile,
  type UsersListResp,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { RefreshCw } from "lucide-react";

type UserRole = "user" | "admin";
type FormUser = UserProfile & {
  role: UserRole;
  personal_space: SkillSpaceConfig;
  team_space: SkillSpaceConfig;
};

const emptySpace = (): SkillSpaceConfig => ({ backend: "viking", viking_api_key: "" });
const emptyUser = (): FormUser => ({
  id: "",
  display_name: "",
  email: "",
  role: "user",
  agent_identities: {},
  agent_subjects: [],
  password: "",
  personal_space: emptySpace(),
  team_space: emptySpace(),
});

function toForm(user?: UserProfile | null): FormUser {
  if (!user) return emptyUser();
  return {
    id: user.id || "",
    display_name: user.display_name || "",
    email: user.email || "",
    role: user.role === "admin" ? "admin" : "user",
    agent_identities: { ...(user.agent_identities || {}) },
    agent_subjects: (user.agent_subjects || []).map((item) => ({ ...item })),
    password: "",
    personal_space: { ...(user.personal_space || emptySpace()), viking_api_key: "" },
    team_space: { ...(user.team_space || emptySpace()), viking_api_key: "" },
  };
}

export default function UsersView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [resp, setResp] = useState<UsersListResp | null>(null);
  const [form, setForm] = useState<FormUser>(() => emptyUser());
  const [selectedId, setSelectedId] = useState("");
  const [saving, setSaving] = useState(false);
  const [savingTeam, setSavingTeam] = useState(false);
  const [loading, setLoading] = useState(false);
  const [integrations, setIntegrations] = useState<AgentIntegration[]>([]);
  const [teamSettings, setTeamSettings] = useState<TeamSettings | null>(null);
  const [teamNameDraft, setTeamNameDraft] = useState("");
  const loaded = useRef(false);

  const users = resp?.users || [];
  const isAdmin = user?.role === "admin";
  const userPager = usePagedItems(users);
  const selectedUser = useMemo(() => users.find((u) => u.id === selectedId) || null, [users, selectedId]);
  const adminCount = users.filter((u) => u.role === "admin").length;
  const openVikingSpaces = users.reduce((n, u) => {
    return n + (u.personal_space?.api_key_present ? 1 : 0) + (u.team_space?.api_key_present ? 1 : 0);
  }, 0);

  const refresh = useCallback(async (notify = false) => {
    setLoading(true);
    try {
      const [data, integrationData, teamData] = await Promise.all([
        api<UsersListResp>("/api/users"),
        api<AgentIntegrationsResp>("/api/agent-integrations"),
        api<TeamSettings>("/api/team-settings"),
      ]);
      setResp(data);
      setIntegrations(integrationData.agents || []);
      setTeamSettings(teamData);
      setTeamNameDraft(teamData.configured_display_name || teamData.display_name || "Team");
      if (!selectedId && !isAdmin && data.users[0]) {
        setSelectedId(data.users[0].id);
        setForm(toForm(data.users[0]));
      }
      if (selectedId && !data.users.some((u) => u.id === selectedId)) {
        setSelectedId("");
        setForm(emptyUser());
      }
      if (notify) toastOk("用户列表已刷新", `${data.users.length} 个用户`);
    } catch (e: any) {
      toastErr("加载用户失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [isAdmin, selectedId]);

  useEffect(() => {
    if (!active) {
      loaded.current = false;
      return;
    }
    if (active && !loaded.current) {
      loaded.current = true;
      refresh(false);
    }
  }, [active, refresh]);

  function selectUser(user: UserProfile) {
    setSelectedId(user.id);
    setForm(toForm(user));
  }

  function newUser() {
    setSelectedId("");
    setForm(emptyUser());
  }

  async function revealSpaceKey(space: "personal" | "team") {
    if (!selectedId) return "";
    const data = await api<SkillSpaceConfig>(
      `/api/users/${encodeURIComponent(selectedId)}/spaces/${space}/secret`
    );
    return data.viking_api_key || "";
  }

  async function saveUser() {
    setSaving(true);
    try {
      const target = isAdmin
        ? "/api/users"
        : `/api/users/${encodeURIComponent(form.id)}/profile`;
      const saved = await api<UserProfile>(target, {
        method: isAdmin ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      toastOk("已保存用户", saved.id);
      setSelectedId(saved.id);
      setForm(toForm(saved));
      await refresh(false);
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  async function saveTeamSettings() {
    const displayName = teamNameDraft.trim();
    if (!displayName) return;
    setSavingTeam(true);
    try {
      const saved = await api<TeamSettings>("/api/team-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      });
      setTeamSettings(saved);
      setTeamNameDraft(saved.configured_display_name || saved.display_name);
      toastOk("已保存团队名称", saved.display_name);
    } catch (e: any) {
      toastErr("保存团队名称失败", e.message);
    } finally {
      setSavingTeam(false);
    }
  }

  async function deleteUser() {
    if (!selectedId) return;
    if (!window.confirm(`确认删除用户「${selectedId}」？`)) return;
    try {
      await api(`/api/users/${encodeURIComponent(selectedId)}`, { method: "DELETE" });
      toastOk("已删除用户", selectedId);
      newUser();
      await refresh(false);
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      <div className="mb-5 flex items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={loading}
          onClick={() => refresh(true)}
        >
          <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
          {loading ? "刷新中…" : "刷新"}
        </Button>
        {isAdmin && <Button size="sm" onClick={newUser}>+ 注册用户</Button>}
      </div>

      <div className="mb-5">
        <Panel
          title="团队身份"
          count={teamSettings?.display_name || "Team"}
          extra={
            teamSettings?.override_source
              ? <Pill tone="amber">{teamSettings.override_source} 覆盖中</Pill>
              : <Pill tone="green">持久化配置</Pill>
          }
        >
          <div className="flex flex-wrap items-end gap-3 p-4">
            <div className="min-w-[280px] flex-1">
              <Field label="团队显示名称">
                <Input
                  value={teamNameDraft}
                  disabled={!isAdmin}
                  maxLength={120}
                  placeholder="Team"
                  onChange={(event) => setTeamNameDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && isAdmin) saveTeamSettings();
                  }}
                />
              </Field>
            </div>
            {isAdmin && (
              <Button
                disabled={savingTeam || !teamNameDraft.trim()}
                onClick={saveTeamSettings}
              >
                {savingTeam ? "保存中…" : "保存团队名称"}
              </Button>
            )}
          </div>
          {teamSettings?.override_source && (
            <div className="border-t border-line px-4 py-3 text-xs text-muted-foreground">
              当前生效值为“{teamSettings.display_name}”。环境变量
              <span className="mx-1 font-mono">{teamSettings.override_source}</span>
              优先于持久化值，移除覆盖后将使用“{teamNameDraft || "Team"}”。
            </div>
          )}
        </Panel>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="注册用户" value={users.length} />
        <StatCard label="管理员" value={adminCount} />
        <StatCard label="已配置云空间" value={openVikingSpaces} />
        <StatCard label="当前选择" value={selectedId || "新用户"} />
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(320px,0.9fr)_minmax(0,1.4fr)]">
        <Panel title="用户列表" count={`(${users.length})`}>
          {loading && !resp ? (
            <Empty>加载中…</Empty>
          ) : !users.length ? (
            <Empty>暂无注册用户，点击右上角「注册用户」创建。</Empty>
          ) : (
            <>
              <ListViewport>
                <table className="w-full border-collapse">
                  <thead>
                    <tr>
                      {["用户", "角色", "个人空间", "团队空间", "更新时间"].map((h) => (
                        <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {userPager.items.map((u) => (
                      <tr
                        key={u.id}
                        role="button"
                        tabIndex={0}
                        aria-label={`编辑用户 ${u.id}`}
                        className={cn("clickable", selectedId === u.id && "bg-muted/70")}
                        onClick={() => selectUser(u)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            selectUser(u);
                          }
                        }}
                      >
                        <td className="border-b border-line px-4 py-2.5 align-top">
                          <div className="mono text-xs font-semibold">{u.id}</div>
                          <div className="mt-1 text-xs text-muted-foreground">{u.display_name || "—"}</div>
                        </td>
                        <td className="border-b border-line px-4 py-2.5 align-top">
                          <Pill tone={u.role === "admin" ? "amber" : "gray"}>{u.role === "admin" ? "管理员" : "一般用户"}</Pill>
                        </td>
                        <td className="border-b border-line px-4 py-2.5 align-top"><SpacePill space={u.personal_space} /></td>
                        <td className="border-b border-line px-4 py-2.5 align-top"><SpacePill space={u.team_space} /></td>
                        <td className="border-b border-line px-4 py-2.5 align-top text-xs text-muted-foreground">{fmtTime(u.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ListViewport>
              <PaginationControls {...userPager} onPageChange={userPager.setPage} />
            </>
          )}
        </Panel>

        <Panel
          title={selectedUser ? `${isAdmin ? "编辑用户" : "个人设置"} · ${selectedUser.id}` : "注册用户"}
          extra={isAdmin && selectedId ? <Button variant="destructive" size="sm" onClick={deleteUser}>删除用户</Button> : null}
        >
          <div className="space-y-5 p-4">
            <div className="grid gap-3.5 md:grid-cols-4">
              <Field label="用户 ID *">
                <Input value={form.id} disabled={!!selectedId || !isAdmin} placeholder="zhangsan" onChange={(e) => setForm({ ...form, id: e.target.value })} />
              </Field>
              <Field label="显示名">
                <Input value={form.display_name || ""} placeholder="张三" onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
              </Field>
              <Field label="邮箱">
                <Input value={form.email || ""} placeholder="name@example.com" onChange={(e) => setForm({ ...form, email: e.target.value })} />
              </Field>
              <Field label="角色">
                <select
                  value={form.role}
                  disabled={!isAdmin}
                  onChange={(e) => setForm({ ...form, role: e.target.value as UserRole })}
                  className="h-8 w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
                >
                  <option value="user">一般用户</option>
                  <option value="admin">管理员</option>
                </select>
              </Field>
            </div>

            <div className="rounded-lg border border-border bg-background/60 p-4">
              <div className="mb-1 text-sm font-bold">Agent 用户身份映射</div>
              <div className="mb-3 text-xs text-muted-foreground">
                V1 按具体 integration + external subject 精确映射；未映射时 Context API 拒绝访问，不再按用户名猜测。
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {integrations.map((integration) => (
                  <Field
                    key={integration.agent_id}
                    label={`${integration.display_name || integration.runtime_type} · ${integration.agent_id}`}
                  >
                    <Input
                      disabled={!isAdmin}
                      value={
                        form.agent_subjects?.find(
                          (item) => item.integration_id === integration.agent_id
                        )?.external_subject || ""
                      }
                      placeholder="Agent 侧稳定 subject"
                      onChange={(event) => {
                        const externalSubject = event.target.value;
                        const next = (form.agent_subjects || []).filter(
                          (item) => item.integration_id !== integration.agent_id
                        );
                        if (externalSubject.trim()) {
                          next.push({
                            integration_id: integration.agent_id,
                            runtime_type: integration.runtime_type,
                            external_subject: externalSubject,
                          });
                        }
                        setForm({ ...form, agent_subjects: next });
                      }}
                    />
                  </Field>
                ))}
              </div>
              {!integrations.length && (
                <div className="rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground">
                  暂无已注册 Agent。Agent 完成 V1 注册后可在此绑定 subject。
                </div>
              )}
              <details className="mt-3 rounded-lg border border-border bg-background">
                <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">
                  Legacy runtime 用户名映射
                </summary>
                <div className="grid gap-3 border-t border-line p-3 md:grid-cols-2">
                  {runtimeOptions(integrations).map((integration) => (
                    <Field
                      key={integration.runtime_type}
                      label={`${integration.display_name || integration.runtime_type} 用户名`}
                    >
                      <Input
                        value={form.agent_identities?.[integration.runtime_type] || ""}
                        placeholder={form.id || "同名用户"}
                        onChange={(event) =>
                          setForm({
                            ...form,
                            agent_identities: {
                              ...(form.agent_identities || {}),
                              [integration.runtime_type]: event.target.value,
                            },
                          })
                        }
                      />
                    </Field>
                  ))}
                </div>
              </details>
            </div>

            <Field label={selectedId ? "重置密码（留空不变）" : "登录密码 *"}>
              <Input
                type="password"
                value={form.password || ""}
                placeholder={selectedId ? "留空保留原密码" : "可使用任意长度"}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
            </Field>

            <div className="grid gap-3.5 md:grid-cols-2">
              <KeyEditor
                title="个人 skill 空间"
                space="personal"
                canReveal={!!selectedId}
                value={form.personal_space}
                onChange={(personal_space) => setForm({ ...form, personal_space })}
                onReveal={revealSpaceKey}
              />
              <KeyEditor
                title="团队 skill 空间"
                space="team"
                canReveal={!!selectedId}
                value={form.team_space}
                onChange={(team_space) => setForm({ ...form, team_space })}
                onReveal={revealSpaceKey}
                disabled={!isAdmin}
              />
            </div>

            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
              不填写 Key 时使用系统默认凭据；普通用户可维护自己的 OpenViking 用户空间和 Agent 用户名。部署、endpoint、account、团队用户与 root prefix 由管理员在“运行状态”页配置。
            </div>

            <div className="flex flex-wrap justify-end gap-2">
              {isAdmin && <Button variant="outline" onClick={newUser}>清空</Button>}
              <Button disabled={saving} onClick={saveUser}>保存用户</Button>
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function SpacePill({ space }: { space?: SkillSpaceConfig }) {
  const cloud = !!space?.api_key_present;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Dot state={cloud ? "on" : "off"} />
      <Pill tone={cloud ? "blue" : "purple"}>
        {space?.viking_user || (cloud ? (space?.inherited_from_admin ? "OpenViking · 继承" : "OpenViking") : "默认空间")}
      </Pill>
    </span>
  );
}

function KeyEditor({
  title,
  space,
  canReveal,
  value,
  onChange,
  onReveal,
  disabled = false,
}: {
  title: string;
  space: "personal" | "team";
  canReveal: boolean;
  value: SkillSpaceConfig;
  onChange: (next: SkillSpaceConfig) => void;
  onReveal: (space: "personal" | "team") => Promise<string>;
  disabled?: boolean;
}) {
  const [visible, setVisible] = useState(false);
  const [revealing, setRevealing] = useState(false);
  const configured = !!value.api_key_present;
  const inherited = !!value.inherited_from_admin;

  async function toggleVisible() {
    if (visible) {
      setVisible(false);
      return;
    }
    if (inherited) {
      setVisible(true);
      return;
    }
    if (configured && canReveal && !value.viking_api_key) {
      setRevealing(true);
      try {
        const key = await onReveal(space);
        onChange({ ...value, viking_api_key: key, clear_viking_api_key: false });
      } catch (e: any) {
        toastErr("读取 Key 失败", e.message);
        return;
      } finally {
        setRevealing(false);
      }
    }
    setVisible(true);
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="text-sm font-bold">{title}</div>
        <SpacePill space={value} />
      </div>
      <Field label={`OpenViking Key${configured ? "（已配置，留空保留）" : ""}`}>
        <div className="flex gap-2">
          <Input
            type={visible ? "text" : "password"}
            disabled={disabled}
            value={value.viking_api_key || ""}
            placeholder={inherited ? "继承管理员团队 Key，不回显明文" : configured ? "已配置，输入新值可替换" : "留空使用默认 OpenViking 空间"}
            onChange={(e) => onChange({ ...value, viking_api_key: e.target.value, clear_viking_api_key: false })}
          />
          <Button
            variant="outline"
            type="button"
            disabled={disabled || revealing || (!configured && !value.viking_api_key)}
            onClick={toggleVisible}
          >
            {visible ? "隐藏" : "显示"}
          </Button>
          <Button
            variant="destructive"
            type="button"
            disabled={disabled}
            onClick={() => {
              setVisible(false);
              onChange({ ...value, viking_api_key: "", clear_viking_api_key: true });
            }}
          >
            清空
          </Button>
        </div>
        <div className="mt-1.5 text-[11px] text-muted-soft">
          默认隐藏；点击“显示”才读取明文。继承管理员团队 Key 时不会回显明文；保存时留空会保留原 Key，清空后保存才删除。
        </div>
      </Field>
      <Field label="OpenViking 用户空间">
        <Input
          value={value.viking_user || ""}
          disabled={disabled}
          placeholder={space === "personal" ? "例如 single_evolve3" : "例如 team_evolve1"}
          onChange={(e) => onChange({ ...value, viking_user: e.target.value })}
        />
      </Field>
    </div>
  );
}

function runtimeOptions(integrations: AgentIntegration[]) {
  const byRuntime = new Map<string, AgentIntegration>();
  for (const integration of integrations) {
    if (integration.runtime_type && !byRuntime.has(integration.runtime_type)) {
      byRuntime.set(integration.runtime_type, integration);
    }
  }
  if (!byRuntime.has("agentshub")) {
    byRuntime.set("agentshub", {
      agent_id: "agentshub",
      runtime_type: "agentshub",
      display_name: "AgentsHub",
    });
  }
  return [...byRuntime.values()].sort((a, b) =>
    a.runtime_type.localeCompare(b.runtime_type)
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}
