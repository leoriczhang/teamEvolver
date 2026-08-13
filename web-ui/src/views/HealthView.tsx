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
import {
  api,
  type EvolveModelSettings,
  type SkillListResp,
  type StatusResp,
  type StorageStatus,
  type UserProfile,
  type UsersListResp,
} from "@/api/client";
import { toastErr } from "@/lib/toast";
import { RefreshCw } from "lucide-react";

type Check = {
  name: string;
  ok: boolean;
  detail: string;
  action?: string;
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
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [h, st, sto, mdl, us, sk, sess, cands] = await Promise.allSettled([
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
      ]);
      setHealth(h.status === "fulfilled" ? h.value : null);
      setStatus(st.status === "fulfilled" ? st.value : null);
      setStorage(sto.status === "fulfilled" ? sto.value : null);
      setModel(mdl.status === "fulfilled" ? mdl.value : null);
      setUsers(us.status === "fulfilled" ? us.value.users || [] : []);
      setSkills(sk.status === "fulfilled" ? sk.value : null);
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
        ? `${(storage.backend || "?").toUpperCase()} · ${storage.reachable ? "可达" : "不可达"}`
        : "无法读取 /storage/status",
      action: storage?.api_key_present ? undefined : "检查 OpenViking Key 或本地存储配置",
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
  ];

  const okCount = checks.filter((c) => c.ok).length;
  const checksPager = usePagedItems(checks);

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

      <Panel title="关键配置概览">
        <div className="grid gap-3 p-4 md:grid-cols-2">
          <Info label="存储后端" value={storage?.backend || "未知"} state={storage?.reachable ? "on" : "err"} />
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
