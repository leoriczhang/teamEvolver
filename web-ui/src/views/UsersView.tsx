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
  type ImportAccountUsersResp,
  type OpenVikingAccountsResp,
  type OpenVikingAccountUsersResp,
  type SkillSpaceConfig,
  type TeamSettings,
  type UserProfile,
  type UsersListResp,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Download, Plus, RefreshCw } from "lucide-react";

type UserRole = "user" | "admin";
type FormUser = UserProfile & {
  role: UserRole;
  personal_space: SkillSpaceConfig;
  team_space: SkillSpaceConfig;
};

const emptySpace = (): SkillSpaceConfig => ({ backend: "viking", viking_user: "" });
const emptyUser = (): FormUser => ({
  id: "",
  display_name: "",
  email: "",
  role: "user",
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
    password: "",
    personal_space: { ...(user.personal_space || emptySpace()) },
    team_space: { ...(user.team_space || emptySpace()) },
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
  const [openVikingAccount, setOpenVikingAccount] = useState("default");
  const [defaultTeamUser, setDefaultTeamUser] = useState("team");
  const [accountOptions, setAccountOptions] = useState<string[]>([]);
  const [browseAccount, setBrowseAccount] = useState("");
  const [accountUsers, setAccountUsers] = useState<OpenVikingAccountUsersResp | null>(null);
  const [loadingAccountUsers, setLoadingAccountUsers] = useState(false);
  const [importSelection, setImportSelection] = useState<Set<string>>(() => new Set());
  const [importing, setImporting] = useState(false);
  const [teamSettings, setTeamSettings] = useState<TeamSettings | null>(null);
  const [teamNameDraft, setTeamNameDraft] = useState("");
  const loaded = useRef(false);

  const users = resp?.users || [];
  const isAdmin = user?.role === "admin";
  const userPager = usePagedItems(users);
  const selectedUser = useMemo(() => users.find((u) => u.id === selectedId) || null, [users, selectedId]);
  // The team-resource name is fixed at registration; Account and Root Key are
  // tenant-level settings.
  const teamBound = !!selectedUser?.team_space?.bound;
  const adminCount = users.filter((u) => u.role === "admin").length;
  const workspaceBindings = users.reduce((n, u) => {
    return n + (u.personal_space?.viking_user ? 1 : 0) + (u.team_space?.viking_user ? 1 : 0);
  }, 0);

  const refresh = useCallback(async (notify = false) => {
    setLoading(true);
    try {
      const [data, teamData] = await Promise.all([
        api<UsersListResp>("/api/users"),
        api<TeamSettings>("/api/team-settings"),
      ]);
      setResp(data);
      setOpenVikingAccount(data.openviking_account || "default");
      setDefaultTeamUser(data.default_team_user || "team");
      setTeamSettings(teamData);
      setTeamNameDraft(teamData.configured_display_name || teamData.display_name || "Team");
      // Account browsing is an admin-only Root Key operation.
      if (isAdmin) {
        try {
          const accts = await api<OpenVikingAccountsResp>("/api/openviking-accounts");
          setAccountOptions(accts.accounts || []);
          setBrowseAccount((prev) => prev || accts.current || accts.accounts?.[0] || "");
        } catch {
          setAccountOptions([]);
        }
      }
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

  const loadAccountUsers = useCallback(
    async (account: string, notify = false) => {
      const target = account.trim();
      if (!target) return;
      setLoadingAccountUsers(true);
      try {
        const data = await api<OpenVikingAccountUsersResp>(
          `/api/openviking-accounts/${encodeURIComponent(target)}/users`
        );
        setAccountUsers(data);
        // Preselect only users that are not already imported locally.
        setImportSelection(
          new Set(
            (data.users || [])
              .filter((u) => !u.imported)
              .map((u) => u.user_id)
          )
        );
        if (data.error) {
          toastErr("读取 account 用户失败", data.error);
        } else if (notify) {
          toastOk("已读取 account 用户", `${data.users.length} 个用户`);
        }
      } catch (e: any) {
        setAccountUsers(null);
        toastErr("读取 account 用户失败", e.message);
      } finally {
        setLoadingAccountUsers(false);
      }
    },
    []
  );

  async function importAccountUsers() {
    if (!browseAccount.trim() || importSelection.size === 0) return;
    setImporting(true);
    try {
      const report = await api<ImportAccountUsersResp>(
        `/api/openviking-accounts/${encodeURIComponent(browseAccount.trim())}/import-users`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_ids: [...importSelection] }),
        }
      );
      toastOk(
        "已导入用户",
        `新增 ${report.imported.length}，已存在 ${report.skipped_existing.length}`
      );
      await Promise.all([refresh(false), loadAccountUsers(browseAccount, false)]);
    } catch (e: any) {
      toastErr("导入失败", e.message);
    } finally {
      setImporting(false);
    }
  }

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

  // Whenever the browsed account changes (admin only), refresh the preview of
  // existing OpenViking users for that account.
  useEffect(() => {
    if (!active || !isAdmin || !browseAccount) return;
    loadAccountUsers(browseAccount, false);
  }, [active, isAdmin, browseAccount, loadAccountUsers]);

  function selectUser(user: UserProfile) {
    setSelectedId(user.id);
    setForm(toForm(user));
  }

  function newUser() {
    setSelectedId("");
    setForm(emptyUser());
  }

  async function saveUser() {
    setSaving(true);
    try {
      const payload: FormUser = {
        ...form,
        personal_space: {
          backend: "viking",
          viking_user: form.personal_space.viking_user?.trim() || form.id.trim(),
        },
        team_space: {
          backend: "viking",
          viking_user: form.team_space.viking_user?.trim() || defaultTeamUser,
        },
      };
      const target = isAdmin
        ? "/api/users"
        : `/api/users/${encodeURIComponent(form.id)}/profile`;
      const saved = await api<UserProfile>(target, {
        method: isAdmin ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
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
        {isAdmin && (
          <Button size="sm" onClick={newUser}>
            <Plus className="size-3.5" />
            注册用户
          </Button>
        )}
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

      {isAdmin && (
        <div className="mb-5">
          <AccountUsersPanel
            accountOptions={accountOptions}
            account={browseAccount}
            onAccountChange={setBrowseAccount}
            data={accountUsers}
            loading={loadingAccountUsers}
            importing={importing}
            selection={importSelection}
            onToggle={(userId, checked) =>
              setImportSelection((prev) => {
                const next = new Set(prev);
                if (checked) next.add(userId);
                else next.delete(userId);
                return next;
              })
            }
            onToggleAll={(checked) =>
              setImportSelection(
                checked
                  ? new Set(
                      (accountUsers?.users || [])
                        .filter((u) => !u.imported)
                        .map((u) => u.user_id)
                    )
                  : new Set()
              )
            }
            onRefresh={() => loadAccountUsers(browseAccount, true)}
            onImport={importAccountUsers}
          />
        </div>
      )}

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="注册用户" value={users.length} />
        <StatCard label="管理员" value={adminCount} />
        <StatCard label="资产空间绑定" value={workspaceBindings} />
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
                      {["用户", "角色", "个人记忆", "团队资源", "更新时间"].map((h) => (
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

            <Field label={selectedId ? "重置密码（留空不变）" : "登录密码 *"}>
              <Input
                type="password"
                value={form.password || ""}
                placeholder={selectedId ? "留空保留原密码" : "可使用任意长度"}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
            </Field>

            <div className="grid gap-3.5 md:grid-cols-2">
              <WorkspaceBinding
                title="个人记忆"
                account={openVikingAccount}
                value={form.personal_space.viking_user || form.id.trim()}
                placeholder="默认与用户 ID 一致"
                status="Trusted"
                hint="服务端使用租户 Root Key；Name 用于 X-OpenViking-User。"
                onChange={(name) =>
                  setForm({
                    ...form,
                    personal_space: {
                      backend: "viking",
                      viking_user: name,
                    },
                  })
                }
              />
              <WorkspaceBinding
                title="团队资源"
                account={openVikingAccount}
                value={form.team_space.viking_user || defaultTeamUser}
                placeholder={`默认 ${defaultTeamUser}`}
                status={teamBound ? "已绑定" : "Trusted"}
                disabled={!isAdmin || teamBound}
                hint={
                  teamBound
                    ? "团队资源 Name 注册后固定；Root Key 由租户配置统一管理。"
                    : "保存后固定团队资源 Name；Root Key 由租户配置统一管理。"
                }
                onChange={(name) =>
                  setForm({
                    ...form,
                    team_space: {
                      backend: "viking",
                      viking_user: name,
                    },
                  })
                }
              />
            </div>

            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
              Trusted 模式只使用租户 Root Key。用户侧仅保存 Account 下的 Name，不保存或下发个人 Key。
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

function AccountUsersPanel({
  accountOptions,
  account,
  onAccountChange,
  data,
  loading,
  importing,
  selection,
  onToggle,
  onToggleAll,
  onRefresh,
  onImport,
}: {
  accountOptions: string[];
  account: string;
  onAccountChange: (account: string) => void;
  data: OpenVikingAccountUsersResp | null;
  loading: boolean;
  importing: boolean;
  selection: Set<string>;
  onToggle: (userId: string, checked: boolean) => void;
  onToggleAll: (checked: boolean) => void;
  onRefresh: () => void;
  onImport: () => void;
}) {
  const rows = data?.users || [];
  const importable = rows.filter((u) => !u.imported);
  const allSelected = importable.length > 0 && importable.every((u) => selection.has(u.user_id));
  const accounts = account && !accountOptions.includes(account)
    ? [account, ...accountOptions]
    : accountOptions;

  return (
    <Panel
      title="从 OpenViking Account 导入用户"
      count={data ? `(${rows.length})` : undefined}
      extra={
        data?.source === "fallback" || data?.error
          ? <Pill tone="amber">OpenViking 不可达</Pill>
          : data
            ? <Pill tone="green">已连接</Pill>
            : null
      }
    >
      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[220px] flex-1">
            <Field label="选择 Account（租户）">
              <select
                value={account}
                onChange={(event) => onAccountChange(event.target.value)}
                className="h-8 w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
              >
                {!accounts.length && <option value="">暂无可用 account</option>}
                {accounts.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            </Field>
          </div>
          <Button variant="outline" size="sm" disabled={loading || !account} onClick={onRefresh}>
            <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
            {loading ? "读取中…" : "读取用户"}
          </Button>
          <Button
            size="sm"
            disabled={importing || selection.size === 0}
            onClick={onImport}
          >
            <Download className="size-3.5" />
            {importing ? "导入中…" : `导入所选 (${selection.size})`}
          </Button>
        </div>

        {data?.error ? (
          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
            无法从 OpenViking 读取该 account 的用户：{data.error}
          </div>
        ) : !rows.length ? (
          <Empty>{loading ? "读取中…" : "该 account 下暂无用户，或尚未读取。"}</Empty>
        ) : (
          <ListViewport>
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="w-10 border-b border-line px-4 py-2.5 text-left">
                    <input
                      type="checkbox"
                      aria-label="全选可导入用户"
                      checked={allSelected}
                      disabled={!importable.length}
                      onChange={(event) => onToggleAll(event.target.checked)}
                    />
                  </th>
                  {["OpenViking 用户", "角色", "状态"].map((h) => (
                    <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.user_id}>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      <input
                        type="checkbox"
                        aria-label={`选择用户 ${u.user_id}`}
                        checked={selection.has(u.user_id)}
                        disabled={u.imported}
                        onChange={(event) => onToggle(u.user_id, event.target.checked)}
                      />
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      <div className="mono text-xs font-semibold">{u.user_id}</div>
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      <Pill tone={u.role === "admin" ? "amber" : "gray"}>
                        {u.role === "admin" ? "管理员" : "一般用户"}
                      </Pill>
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      {u.imported
                        ? <Pill tone="green">已导入</Pill>
                        : <Pill tone="blue">可导入</Pill>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ListViewport>
        )}
        <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
          导入为增量操作：已存在的 teamEvolver 用户不会被覆盖；角色沿用 OpenViking account 内的角色（该 account 的管理员即为对应租户管理员）。
        </div>
      </div>
    </Panel>
  );
}

function SpacePill({ space }: { space?: SkillSpaceConfig }) {
  const bound = !!space?.viking_user;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Dot state={bound ? "on" : "off"} />
      <Pill tone={bound ? "blue" : "purple"}>
        {space?.viking_user || "未绑定"}
      </Pill>
    </span>
  );
}

function WorkspaceBinding({
  title,
  account,
  value,
  placeholder,
  status,
  disabled = false,
  hint,
  onChange,
}: {
  title: string;
  account: string;
  value: string;
  placeholder: string;
  status: string;
  disabled?: boolean;
  hint?: string;
  onChange?: (value: string) => void;
}) {
  return (
    <div className="border border-border bg-background/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="text-sm font-bold">{title}</div>
        <Pill tone="green">{status}</Pill>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Account">
          <Input value={account} disabled />
        </Field>
        <Field label="Name">
          <Input
            value={value}
            disabled={disabled}
            placeholder={placeholder}
            onChange={(event) => onChange?.(event.target.value)}
          />
        </Field>
      </div>
      {hint ? (
        <div className="mt-1.5 text-[11px] text-muted-soft">{hint}</div>
      ) : null}
    </div>
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
