import { useCallback, useEffect, useState } from "react";
import { api, getTenantConfig, listTenants, rotateTenantToken, updateTenantConfig } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Panel } from "@/components/common";
import { toastErr, toastOk } from "@/lib/toast";
import { Copy, KeyRound, RefreshCw, Save, UserCheck, UserPlus, UserX, X } from "lucide-react";

type Member = {
  user_id: string;
  display_name: string;
  role: "member" | "tenant_admin";
  status: "active" | "disabled";
};

export default function MembersView({ active, tenantId }: { active: boolean; tenantId: string }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<Member["role"]>("member");
  const [busy, setBusy] = useState(false);
  const [canRotate, setCanRotate] = useState(false);
  const [canConfigure, setCanConfigure] = useState(false);
  const [token, setToken] = useState("");
  const [concurrentLimit, setConcurrentLimit] = useState("");
  const [dailyLimit, setDailyLimit] = useState("");
  const base = `/api/tenants/${encodeURIComponent(tenantId)}/members`;
  const refresh = useCallback(async () => {
    try {
      const [data, tenants, config] = await Promise.all([
        api<{ members: Member[] }>(base), listTenants(), getTenantConfig(tenantId),
      ]);
      setMembers(data.members);
      setCanRotate(tenants.mode === "postgres" && tenantId !== "default");
      setCanConfigure(tenants.mode === "postgres");
      setConcurrentLimit(String(config.config_overrides.max_concurrent_sessions || ""));
      setDailyLimit(String(config.config_overrides.max_evolve_per_day || ""));
    } catch (e: any) { toastErr("加载成员失败", e.message); }
  }, [base, tenantId]);
  useEffect(() => { if (active && tenantId) void refresh(); }, [active, tenantId, refresh]);

  async function save(id: string, nextRole: Member["role"], status: Member["status"]) {
    setBusy(true);
    try {
      await api(`${base}/${encodeURIComponent(id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: nextRole, status }),
      });
      setUserId("");
      toastOk("成员关系已保存");
      await refresh();
      window.dispatchEvent(new Event("console-auth-changed"));
    } catch (e: any) { toastErr("保存失败", e.message); }
    finally { setBusy(false); }
  }
  async function rotate() {
    if (!window.confirm("轮换后旧租户 Key 立即失效，确认继续？")) return;
    setBusy(true);
    try { setToken((await rotateTenantToken(tenantId)).agent_token); }
    catch (e: any) { toastErr("轮换失败", e.message); }
    finally { setBusy(false); }
  }
  async function saveQuotas() {
    const parse = (raw: string) => {
      if (!raw.trim() || Number(raw) === 0) return null;
      const value = Number(raw);
      if (!Number.isInteger(value) || value < 1) throw new Error("配额必须是正整数，留空或 0 表示不限制");
      return value;
    };
    setBusy(true);
    try {
      await updateTenantConfig(tenantId, {
        max_concurrent_sessions: parse(concurrentLimit),
        max_evolve_per_day: parse(dailyLimit),
      });
      toastOk("租户配额已保存");
    } catch (e: any) { toastErr("保存配额失败", e.message); }
    finally { setBusy(false); }
  }
  return <div className="space-y-5 p-6">
    <Panel title="添加成员" count={tenantId}>
      <div className="space-y-3 p-4">
        <p className="text-sm text-muted-foreground">输入已注册账号的用户 ID。用户可在账户菜单查看自己的 ID。</p>
        <div className="flex flex-wrap gap-3">
          <Input className="max-w-xs" aria-label="成员用户 ID" placeholder="用户 ID" value={userId} onChange={e => setUserId(e.target.value)} />
          <select aria-label="新成员角色" className="rounded border px-3" value={role} onChange={e => setRole(e.target.value as Member["role"])}>
            <option value="member">普通成员</option><option value="tenant_admin">租户管理员</option>
          </select>
          <Button disabled={busy || !userId.trim()} onClick={() => save(userId.trim(), role, "active")}>
            <UserPlus className="size-4" />添加成员
          </Button>
        </div>
      </div>
    </Panel>
    <Panel title="租户成员" count={`${members.length} 人`} extra={<Button variant="outline" onClick={refresh}><RefreshCw className="size-4" />刷新</Button>}>
      <table className="w-full text-sm">
        <thead><tr className="border-b text-left"><th className="p-3">用户</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{members.map(m => <tr className="border-b" key={m.user_id}>
          <td className="p-3">{m.display_name}<span className="ml-2 text-muted-foreground">{m.user_id}</span></td>
          <td><select aria-label={`${m.user_id} 的角色`} disabled={busy} value={m.role}
            onChange={e => save(m.user_id, e.target.value as Member["role"], m.status)}>
            <option value="member">普通成员</option><option value="tenant_admin">租户管理员</option>
          </select></td>
          <td>{m.status === "active" ? "正常" : "已停用"}</td>
          <td><Button size="sm" variant="outline" disabled={busy} onClick={() => save(m.user_id, m.role, m.status === "active" ? "disabled" : "active")}>
            {m.status === "active" ? <UserX className="size-4" /> : <UserCheck className="size-4" />}
            {m.status === "active" ? "停用成员" : "恢复成员"}
          </Button></td>
        </tr>)}</tbody>
      </table>
      {!members.length && <p className="p-4 text-sm text-muted-foreground">暂无成员，请先指定租户管理员。</p>}
    </Panel>
    {canConfigure && <Panel title="租户运行配额">
      <div className="flex flex-wrap items-end gap-3 p-4">
        <label className="text-sm">并发 Session 上限
          <Input className="mt-1" type="number" min="0" value={concurrentLimit} placeholder="不限制" onChange={e => setConcurrentLimit(e.target.value)} />
        </label>
        <label className="text-sm">每日 Evolution 上限
          <Input className="mt-1" type="number" min="0" value={dailyLimit} placeholder="不限制" onChange={e => setDailyLimit(e.target.value)} />
        </label>
        <Button disabled={busy} onClick={saveQuotas}><Save className="size-4" />保存配额</Button>
      </div>
    </Panel>}
    <Panel title="Agent 接入凭证">
      <div className="space-y-3 p-4 text-sm">
        <p>Agent 使用租户 Key 和声明的 user_id 接入。控制台成员关系与 Agent 身份独立。</p>
        {canRotate ? <Button variant="outline" disabled={busy} onClick={rotate}><KeyRound className="size-4" />轮换租户 Key</Button>
          : <p className="text-muted-foreground">default 租户的接入凭证由平台管理员配置。</p>}
        {token && <div className="space-y-3 rounded border border-amber-300 bg-amber-50 p-4">
          <p>新 Key 仅展示一次，请保存并更新 Agent 配置。</p>
          <code className="block break-all">{token}</code>
          <div className="flex gap-2">
            <Button variant="outline" onClick={async () => {
              try { await navigator.clipboard.writeText(token); toastOk("租户 Key 已复制"); }
              catch { toastErr("复制失败", "请手动选择复制"); }
            }}><Copy className="size-4" />复制</Button>
            <Button variant="outline" onClick={() => setToken("")}><X className="size-4" />已保存，关闭</Button>
          </div>
        </div>}
      </div>
    </Panel>
  </div>;
}
