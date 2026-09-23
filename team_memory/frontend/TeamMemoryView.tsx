import { useCallback, useEffect, useRef, useState } from "react";
import { CheckSquare, Copy, Play, RefreshCw, Save, Users } from "lucide-react";
import { api, type AggregationSettings, type UserProfile } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toastErr, toastOk } from "@/lib/toast";

type Pipeline = "both" | "aggregate" | "maintain";
type SkillStage = "aggregation" | "maintenance";
type Skill = { body: string; skill_uri: string; revision?: string; source?: string };
type Run = {
  task_id: string; status: string; stage: string; target_uri: string; account_id: string;
  snapshot_uri?: string; error?: string; pipeline?: Pipeline;
  upstream_tasks?: Record<string, string>;
  group_counts?: Record<string, number>;
  groups: { group_key: string; status: string; detail?: string }[];
};
const STAGES: Record<string, string> = {
  pending: "排队中", preparing: "准备 Skill", staging: "采集快照",
  aggregation: "聚合", snapshot: "备份", maintenance: "DreamCycle 维护", completed: "已完成",
};
const STATUS: Record<string, string> = {
  pending: "排队中", running: "运行中", completed: "已完成", failed: "失败",
  ok: "完成", skipped: "复用",
};

function SkillEditor({ stage, account, active, onSaved }: {
  stage: SkillStage; account: string; active: boolean; onSaved: () => void;
}) {
  const [skill, setSkill] = useState<Skill | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const title = stage === "aggregation" ? "聚合 Skill" : "DreamCycle Skill";
  const endpoint = `/api/aggregation/okf-skill?stage=${stage}&account_id=${encodeURIComponent(account)}`;
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setSkill(null);
    setDraft("");
    setError("");
    api<Skill>(endpoint).then(data => {
      if (!cancelled) { setSkill(data); setDraft(data.body); }
    }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [endpoint, active, refresh]);
  async function save() {
    setSaving(true);
    try {
      const next = await api<Skill>(endpoint, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body: draft, version_message: `Update ${stage} Skill` }),
      });
      setSkill(next); setDraft(next.body); onSaved(); toastOk("已保存", title);
    } catch (e: any) { toastErr("Skill 保存失败", e.message); }
    finally { setSaving(false); }
  }
  return <section className="min-w-0 rounded-md border border-border">
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      <div className="flex gap-1">
        <Button variant="ghost" size="sm" title={`刷新${title}`} aria-label={`刷新${title}`}
          disabled={saving} onClick={() => setRefresh(n => n + 1)}><RefreshCw className="size-4" /></Button>
        <Button size="sm" title={`保存${title}`} aria-label={`保存${title}`}
          disabled={!skill || !draft.trim() || saving || draft === skill.body} onClick={save}>
          <Save className="size-4" />{saving ? "保存中" : "保存"}
        </Button>
      </div>
    </div>
    <div className="p-3">
      <div className="mb-2 break-all text-xs text-muted-foreground">{skill?.skill_uri || title}</div>
      {error ? <div role="alert" className="text-sm text-red-600">{error}</div> :
        <textarea aria-label={`${title}内容`} value={draft} disabled={!skill || saving}
          onChange={event => setDraft(event.target.value)} spellCheck={false}
          className="h-80 w-full resize-y rounded border border-border bg-surface p-3 font-mono text-xs leading-relaxed"
          placeholder={skill ? "" : "加载中"} />}
    </div>
  </section>;
}

export default function TeamMemoryView({ active, user }: { active: boolean; user?: UserProfile | null }) {
  const isAdmin = user?.role === "admin";
  const [settings, setSettings] = useState<AggregationSettings | null>(null);
  const [accountInput, setAccountInput] = useState("");
  const [account, setAccount] = useState("");
  const [target, setTarget] = useState("");
  const [aggregationSkill, setAggregationSkill] = useState("");
  const [maintenanceSkill, setMaintenanceSkill] = useState("");
  const [skillVersion, setSkillVersion] = useState(0);
  const [pipeline, setPipeline] = useState<Pipeline>("both");
  const [mode, setMode] = useState("incremental");
  const [users, setUsers] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [listing, setListing] = useState(false);
  const scopeRef = useRef(0);
  const running = run?.status === "pending" || run?.status === "running";
  const refresh = useCallback(async () => {
    const data = await api<{ runs: Run[] }>("/api/aggregation/runs");
    setRun(data.runs[0] || null);
  }, []);
  useEffect(() => {
    if (!active || !isAdmin) return;
    let cancelled = false;
    api<AggregationSettings>("/api/aggregation/settings").then(data => {
      if (cancelled) return;
      setSettings(data); setTarget(data.target_root);
      setAggregationSkill(data.okf_skill_uri); setMaintenanceSkill(data.maintenance_skill_uri);
    }).catch(e => { if (!cancelled) setError(e.message); });
    refresh().catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; scopeRef.current++; };
  }, [active, isAdmin, refresh]);
  useEffect(() => {
    if (!active || !running || !run) return;
    let cancelled = false;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const data = await api<Run>(`/api/aggregation/status/${run.task_id}`);
        if (!cancelled) { setRun(data); setError(""); }
      } catch (e: any) { if (!cancelled) setError(e.message); }
      finally { pending = false; }
    }, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active, running, run?.task_id]);

  async function listUsers() {
    const scope = ++scopeRef.current;
    const nextAccount = accountInput.trim();
    setAccount(nextAccount); setUsers([]); setSelected(new Set()); setListing(true); setError("");
    try {
      const data = await api<{ users: string[] }>("/api/aggregation/users", {
        method: "POST", body: JSON.stringify({ account_id: nextAccount || undefined }),
      });
      if (scope === scopeRef.current) { setUsers(data.users); setSelected(new Set(data.users)); }
    } catch (e: any) { if (scope === scopeRef.current) setError(e.message); }
    finally { if (scope === scopeRef.current) setListing(false); }
  }
  async function start() {
    setBusy(true); setError("");
    try {
      const data = await api<Run>("/api/aggregation/run", {
        method: "POST", body: JSON.stringify({
          account_id: accountInput.trim() || undefined, target_uri: target.trim(), pipeline, mode,
          user_ids: pipeline === "maintain" ? undefined : [...selected],
        }),
      });
      setAccount(accountInput.trim()); setRun(data);
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function saveSettings() {
    setBusy(true); setError("");
    try {
      const data = await api<AggregationSettings>("/api/aggregation/settings", {
        method: "POST", body: JSON.stringify({
          okf_skill_uri: aggregationSkill.trim(), maintenance_skill_uri: maintenanceSkill.trim(),
        }),
      });
      setSettings(data); setAggregationSkill(data.okf_skill_uri); setMaintenanceSkill(data.maintenance_skill_uri);
      setSkillVersion(n => n + 1); toastOk("已保存", "阶段 Skill");
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }
  if (!isAdmin) return <div className="p-4 text-sm text-muted-foreground">团队 Memory 管理需要管理员权限。</div>;
  const selectedScope = account === accountInput.trim();
  const filtered = users.filter(id => id.toLowerCase().includes(filter.toLowerCase()));
  return <div className="space-y-5 px-4 py-4 sm:px-5">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-base font-semibold">团队 Memory</h2>
      <Button variant="outline" size="sm" aria-label="刷新任务" title="刷新任务"
        onClick={() => refresh().catch(e => setError(e.message))}><RefreshCw className="size-4" /></Button>
    </header>
    {error && <div role="alert" className="break-words text-sm text-red-600">{error}</div>}
    <section className="space-y-3 border-b border-border pb-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-xs font-semibold">Account ID<Input className="mt-1" value={accountInput}
          disabled={running || busy} onBlur={() => { if (pipeline === "maintain") setAccount(accountInput.trim()); }}
          onChange={e => { scopeRef.current++; setListing(false); setAccountInput(e.target.value); }} placeholder="默认 Account" /></label>
        <label className="text-xs font-semibold">团队 Memory 目录<Input className="mt-1 font-mono" value={target}
          disabled={running || busy} onChange={e => setTarget(e.target.value)} /></label>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="运行阶段" className="flex flex-wrap gap-1">
          {([["both", "聚合 + DreamCycle"], ["aggregate", "仅聚合"], ["maintain", "仅 DreamCycle"]] as const).map(([key, title]) =>
            <Button key={key} size="sm" variant={pipeline === key ? "default" : "outline"}
              aria-pressed={pipeline === key} disabled={running || busy} onClick={() => setPipeline(key)}>{title}</Button>)}
        </div>
        <label className="flex items-center gap-2 text-xs">模式<select aria-label="运行模式" value={mode}
          disabled={running || busy} onChange={e => setMode(e.target.value)} className="h-8 rounded border border-border bg-surface px-2">
          <option value="incremental">增量</option><option value="full">全量</option>
        </select></label>
        <Button disabled={busy || running || !target.trim() || !settings ||
          (pipeline !== "maintain" && (!selected.size || !selectedScope))} onClick={start}>
          <Play className="size-4" />{busy ? "处理中" : running ? "运行中" : "运行"}
        </Button>
      </div>
      {pipeline !== "maintain" && <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={listing || running} onClick={listUsers}><Users className="size-4" />{listing ? "加载中" : "加载成员"}</Button>
          <span className="text-xs text-muted-foreground">已选 {selectedScope ? selected.size : 0} / {users.length}</span>
          <Input aria-label="筛选成员" value={filter} onChange={e => setFilter(e.target.value)} className="h-8 w-44" placeholder="筛选成员" />
          <Button variant="ghost" size="sm" disabled={running || !selectedScope} onClick={() => setSelected(new Set(users))}><CheckSquare className="size-4" />全选</Button>
          <Button variant="ghost" size="sm" disabled={running || !selectedScope} onClick={() => setSelected(new Set())}>清空</Button>
        </div>
        {selectedScope && <div className="grid max-h-44 grid-cols-2 gap-2 overflow-auto sm:grid-cols-4">
          {filtered.slice(0, 500).map(id => <label key={id} className="flex min-w-0 items-center gap-2 text-xs">
            <input type="checkbox" disabled={running} checked={selected.has(id)} onChange={e => setSelected(current => {
              const next = new Set(current); e.target.checked ? next.add(id) : next.delete(id); return next;
            })} /><span className="truncate" title={id}>{id}</span>
          </label>)}
        </div>}
      </div>}
    </section>
    {run && <section className="space-y-2 border-b border-border pb-5">
      <div className="flex flex-wrap gap-3 text-sm font-semibold"><h3>最近任务</h3>
        <span>{STATUS[run.status] || run.status}</span><span>{STAGES[run.stage] || run.stage}</span></div>
      <div className="break-all text-xs text-muted-foreground">{run.account_id} / {run.target_uri}</div>
      {run.error && <div role="alert" className="break-words text-sm text-red-600">{run.error}</div>}
      {Object.keys(run.upstream_tasks || {}).length > 0 && <div className="text-xs text-amber-700">OpenViking 任务尚未确认结束，目标目录仍锁定。</div>}
      {run.snapshot_uri && <div className="flex min-w-0 items-start gap-2 text-xs text-muted-foreground"><Copy className="size-4 shrink-0" /><span className="break-all">{run.snapshot_uri}</span></div>}
      <div className="max-h-56 overflow-auto"><table className="w-full table-fixed text-left text-xs">
        <thead><tr><th className="w-1/4 py-2">分组</th><th className="w-20">状态</th><th>结果</th></tr></thead>
        <tbody>{run.groups?.map((group, index) => <tr key={`${group.group_key}-${index}`} className="border-t border-border">
          <td className="break-all py-2 pr-2">{group.group_key}</td><td>{STATUS[group.status] || group.status}</td>
          <td className="break-words">{group.detail || ""}</td>
        </tr>)}</tbody>
      </table></div>
    </section>}
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">阶段 Skill</h3>
        <Button variant="outline" size="sm" disabled={!settings || running || busy ||
          (aggregationSkill === settings.okf_skill_uri && maintenanceSkill === settings.maintenance_skill_uri)} onClick={saveSettings}><Save className="size-4" />保存配置</Button></div>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-xs">聚合 Skill URI<Input className="mt-1 font-mono" value={aggregationSkill} disabled={running || busy} onChange={e => setAggregationSkill(e.target.value)} /></label>
        <label className="text-xs">DreamCycle Skill URI<Input className="mt-1 font-mono" value={maintenanceSkill} disabled={running || busy} onChange={e => setMaintenanceSkill(e.target.value)} /></label>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {(["aggregation", "maintenance"] as const).map(stage => <SkillEditor key={`${stage}-${skillVersion}`} stage={stage}
          active={active && selectedScope} account={account} onSaved={() => {}} />)}
      </div>
    </section>
  </div>;
}
