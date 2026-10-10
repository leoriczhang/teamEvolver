import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { toastErr } from "@/lib/toast";
import { RefreshCw } from "lucide-react";

type Event = { id: string; created_at: number; actor: string; action: string; tenant_id: string; details: Record<string, unknown> };

export default function IdentityAuditView({ active }: { active: boolean }) {
  const [events, setEvents] = useState<Event[]>([]);
  const refresh = useCallback(async () => {
    try { setEvents((await api<{ events: Event[] }>("/api/identity/audit?limit=200")).events); }
    catch (e: any) { toastErr("加载审计失败", e.message); }
  }, []);
  useEffect(() => { if (active) void refresh(); }, [active, refresh]);
  return <div className="space-y-4 p-6">
    <div className="flex items-center justify-between"><span className="text-sm text-muted-foreground">最近 200 条权限记录</span><Button variant="outline" onClick={refresh}><RefreshCw className="size-4" />刷新</Button></div>
    <table className="w-full text-sm">
      <thead><tr className="border-b text-left">{["时间", "操作人", "租户", "操作", "详情"].map(h => <th className="p-3" key={h}>{h}</th>)}</tr></thead>
      <tbody>{events.map(e => <tr key={e.id} className="border-b align-top">
        <td className="p-3 whitespace-nowrap">{new Date(e.created_at * 1000).toLocaleString()}</td><td className="p-3">{e.actor}</td>
        <td className="p-3">{e.tenant_id || "平台"}</td><td className="p-3">{e.action}</td>
        <td className="max-w-md break-all p-3 text-xs">{JSON.stringify(e.details)}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}
