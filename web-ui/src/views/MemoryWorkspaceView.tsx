import { useEffect, useState } from "react";
import { Bug, Database, Search } from "lucide-react";
import { api, type UserProfile, type UsersListResp } from "@/api/client";
import { Empty, Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toastErr } from "@/lib/toast";
import { cn } from "@/lib/utils";
import OpenVikingWorkspaceShell from "@/views/OpenVikingWorkspaceShell";

type MemoryDebugResult = {
  items: Array<{ scope: string; space: string; title: string; path_alias: string; l0?: string; l1?: string; score?: number }>;
  agent_context: string;
  budget: { used_items: number; used_chars: number; max_items: number; max_chars: number; truncated: boolean };
};

export default function MemoryWorkspaceView({ active, user }: { active: boolean; user?: UserProfile | null }) {
  const [tab, setTab] = useState<"assets" | "debug">("assets");
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [userId, setUserId] = useState(user?.id || "");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<MemoryDebugResult | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!active) return;
    api<UsersListResp>("/api/users").then((data) => {
      setUsers(data.users || []);
      setUserId((current) => current || user?.id || data.users?.[0]?.id || "");
    }).catch((error) => toastErr("加载用户失败", error.message));
  }, [active, user?.id]);

  async function debug() {
    if (!query.trim() || !userId) return;
    setLoading(true);
    try {
      setResult(await api<MemoryDebugResult>("/api/openviking/memory/debug", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, query, max_items: 12, max_chars: 16000 }),
      }));
    } catch (error: any) { toastErr("Memory 调试失败", error.message); }
    finally { setLoading(false); }
  }

  return <div>
    <div className="section-tabs pt-2.5"><div className="flex gap-1.5" role="tablist">
      {[{ key: "assets", label: "Memory 资产", icon: Database }, { key: "debug", label: "Agent 注入调试", icon: Bug }].map(({ key, label, icon: Icon }) =>
        <button key={key} type="button" onClick={() => setTab(key as "assets" | "debug")}
          className={cn("flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-bold", tab === key ? "border-accent text-foreground" : "border-transparent text-muted-foreground")}><Icon className="size-4" />{label}</button>)}
    </div></div>
    <div className={cn(tab !== "assets" && "hidden")}><OpenVikingWorkspaceShell active={active && tab === "assets"} mode="memory" user={user} /></div>
    <div className={cn(tab !== "debug" && "hidden", "mx-auto max-w-[1200px] px-[22px] py-[22px]")}>
      <Panel title="Memory 注入调试" count="同时检索个人与团队 Memory，使用 Agent Context 同款预算">
        <div className="flex flex-wrap items-end gap-3 p-4">
          <label className="text-xs font-semibold">用户<select value={userId} onChange={(e) => setUserId(e.target.value)} className="mt-1 block h-9 min-w-48 rounded-md border bg-background px-2">{users.map((item) => <option key={item.id} value={item.id}>{item.display_name || item.id}</option>)}</select></label>
          <label className="min-w-[320px] flex-1 text-xs font-semibold">Agent Query<Input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && debug()} className="mt-1" placeholder="输入 Agent 当前任务，查看会召回哪些 Memory" /></label>
          <Button onClick={debug} disabled={loading || !query.trim()}><Search className="size-4" />{loading ? "检索中…" : "模拟注入"}</Button>
        </div>
      </Panel>
      {result ? <div className="mt-5 grid gap-5 lg:grid-cols-[1fr_1.15fr]">
        <Panel title="检索命中" count={`${result.budget.used_items} 条 · ${result.budget.used_chars} 字符`}><div className="max-h-[620px] divide-y overflow-auto">{result.items.map((item) => <div key={`${item.scope}-${item.path_alias}`} className="p-4"><div className="flex items-center gap-2"><Pill tone={item.space === "personal" ? "blue" : "purple"}>{item.space === "personal" ? "个人" : "团队"}</Pill><strong>{item.title}</strong>{item.score != null && <span className="ml-auto text-xs text-muted-foreground">score {item.score}</span>}</div><div className="mt-1 font-mono text-[10px] text-muted-foreground">{item.path_alias}</div><p className="mt-2 whitespace-pre-wrap text-xs leading-6">{item.l0 || item.l1 || "无摘要"}</p></div>)}</div></Panel>
        <Panel title="实际喂给 Agent 的上下文" extra={<Pill tone={result.budget.truncated ? "amber" : "green"}>{result.budget.truncated ? "已截断" : "预算内"}</Pill>}><pre className="max-h-[620px] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-6">{result.agent_context || "（无注入内容）"}</pre></Panel>
      </div> : <div className="mt-5"><Panel title="调试结果"><Empty>输入 Agent Query 后，可同时查看个人和团队 Memory 命中，以及最终注入文本。</Empty></Panel></div>}
    </div>
  </div>;
}
