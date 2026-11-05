import { Empty, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PlugZap, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { followMAJob, maConnection, maConnections, maReleases, maResources, saveConnection, startMAJob,
  type MAConnection, type MAJob, type MARelease, type MAResource } from "./api/client";
import ConnectionForm from "./ConnectionForm";
import ReleaseHistory, { maTime } from "./ReleaseHistory";
import ResourceBindings from "./ResourceBindings";

const labels: Record<string, string> = {
  ready: "运行正常", discovering: "发现中", importing: "导入中", syncing: "同步中", error: "需要处理", drift: "版本漂移",
};
export default function ManagedAgentsView({ active, canManage = false }: { active: boolean; canManage?: boolean }) {
  const [connections, setConnections] = useState<MAConnection[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<MAConnection | null>(null);
  const [resources, setResources] = useState<MAResource[]>([]);
  const [releases, setReleases] = useState<MARelease[]>([]);
  const [form, setForm] = useState<"create" | "edit" | null>(null);
  const [tab, setTab] = useState<"resources" | "sessions" | "releases">("resources");
  const [error, setError] = useState("");
  const [jobStatus, setJobStatus] = useState("");
  const [sessionIds, setSessionIds] = useState("");
  const [busy, setBusy] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const refresh = useCallback(async () => {
    try {
      const list = await maConnections();
      setConnections(list.connections);
      if (!selected && list.connections.length) setSelected(list.connections[0].connection_id);
      if (selected) {
        const connection = await maConnection(selected);
        const resource = await maResources(selected);
        const history = await maReleases(selected);
        if (selectedRef.current === selected) {
          setDetail(connection); setResources(resource.resources); setReleases(history.releases);
        }
      }
    } catch (e) { setError(e instanceof Error ? e.message : "加载失败"); }
  }, [selected]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, [active]);
  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 15_000);
    return () => clearInterval(timer);
  }, [active, refresh]);
  async function track(job: MAJob) {
    setBusy(true); setError(""); setJobStatus("已受理 · " + job.job_id);
    try {
      const signal = lifetime.current?.signal || new AbortController().signal;
      await followMAJob(job, signal, status => setJobStatus(`${job.job_id} · ${status}`));
      if (!signal.aborted) { setJobStatus("任务完成 · " + job.job_id); await refresh(); }
    } catch (e) { setError(e instanceof Error ? e.message : "任务失败"); await refresh(); }
    finally { setBusy(false); }
  }
  async function run(kind: "discover" | "collect" | "reconcile") {
    if (!selected || busy) return;
    setBusy(true); setError("");
    try {
      const body = kind === "collect" && sessionIds.trim()
        ? { session_ids: sessionIds.split(/[\s,]+/).filter(Boolean) } : {};
      await track(await startMAJob(selected, kind, body));
    } catch (e) { setError(e instanceof Error ? e.message : "受理失败"); }
    finally { setBusy(false); }
  }
  async function toggle() {
    if (!detail) return;
    setBusy(true); setError("");
    try {
      await saveConnection({ expected_revision: detail.revision, enabled: !detail.enabled }, selected);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "更新失败"); }
    finally { setBusy(false); }
  }
  return <div className="mx-auto max-w-[1320px] space-y-5 p-5">
    <div className="flex items-center justify-between gap-3">
      <p className="text-sm text-muted-foreground">连接真实会话、团队 Skill 和云端发布版本。</p>
      <div className="flex gap-2"><Button variant="outline" size="sm" onClick={refresh}>
        <RefreshCw className="size-4" />刷新</Button>{canManage && <Button size="sm" onClick={() => setForm("create")}>
          <Plus className="size-4" />新建连接</Button>}</div>
    </div>
    {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error}</div>}
    {jobStatus && <p role="status" className="break-all rounded-lg border p-3 font-mono text-xs">{jobStatus}</p>}
    {!connections.length ? <Empty><PlugZap className="mx-auto mb-3 size-8" />尚未接入 MA。
      {canManage && <p className="mt-2">添加入口 Agent 后，系统自动发现成员并导入自定义 Skill。</p>}</Empty> :
      <div className="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
        <nav aria-label="MA 连接列表" className="space-y-2">{connections.map(connection =>
          <button key={connection.connection_id} aria-current={selected === connection.connection_id ? "page" : undefined}
            onClick={() => { setSelected(connection.connection_id); setDetail(null); }}
            className={`w-full rounded-lg border p-4 text-left transition-colors ${selected === connection.connection_id
              ? "border-accent bg-accent-soft" : "hover:bg-muted"}`}>
            <div className="font-semibold text-sm">{connection.config.display_name}</div>
            <div className="mt-2 flex items-center justify-between text-xs">
              <Pill tone={!connection.enabled ? "gray" : connection.status === "ready" ? "green" : "amber"}>
                {!connection.enabled ? "已暂停" : labels[connection.status] || connection.status}</Pill>
              <span className="text-muted-foreground">{connection.detail.managed_skill_count ?? "—"} Skills</span>
            </div></button>)}</nav>
        {detail && <div className="min-w-0 space-y-4">
          <div className="rounded-xl border bg-surface-subtle p-5">
            <div className="flex flex-wrap justify-between gap-3"><div>
              <h2 className="text-xl font-bold">{detail.config.display_name}</h2>
              <p className="mt-1 font-mono text-xs text-muted-foreground">{detail.config.entry_agent_id}</p></div>
              {canManage && <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => setForm("edit")}>编辑</Button>
                <Button size="sm" variant="outline" disabled={busy} onClick={toggle}>{detail.enabled ? "暂停" : "恢复"}</Button>
              </div>}</div>
            <div className="mt-5 grid gap-3 text-xs sm:grid-cols-3">
              <div>最近采集<p className="mt-1 font-semibold">{maTime(detail.last_collected_at)}</p></div>
              <div>最近同步<p className="mt-1 font-semibold">{maTime(detail.last_synced_at)}</p></div>
              <div>True Replay<p className="mt-1 font-semibold">{detail.detail.replay_capability?.supported
                ? "可执行" : detail.detail.replay_capability?.reason || "待检查"}</p></div>
            </div>
            <p className="mt-3 text-xs text-muted-foreground">MA 凭证{detail.config.ma_credential_configured ? "已配置" : "未配置"}
              · 租户 Key {detail.config.tenant_credential_configured ? "已配置" : "未配置"}</p>
            {detail.detail.error && <p className="mt-3 text-sm text-destructive">{detail.detail.error}</p>}
          </div>
          {canManage && <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run("discover")}>发现／重新检查</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run("reconcile")}>同步／恢复对账</Button>
            <Input className="w-[240px]" aria-label="补采 Session ID" placeholder="Session ID（留空按时间扫描）"
              value={sessionIds} onChange={e => setSessionIds(e.target.value)} />
            <Button size="sm" disabled={busy} onClick={() => run("collect")}>补采会话</Button>
            <a className="ml-auto text-xs text-accent underline" href="?view=dashboard&tab=candidates">查看 Candidate 评审 →</a>
          </div>}
          <div role="tablist" aria-label="连接详情" className="flex gap-5 border-b">
            {(["resources", "sessions", "releases"] as const).map((value, index) =>
              <button key={value} role="tab" aria-selected={tab === value} onClick={() => setTab(value)}
                className={`border-b-2 px-1 py-3 text-sm ${tab === value ? "border-accent font-semibold" : "border-transparent text-muted-foreground"}`}>
                {["成员与 Skill", "会话采集", "发布历史"][index]}</button>)}</div>
          {tab === "resources" && <ResourceBindings connectionId={selected} resources={resources} canManage={canManage} onSaved={refresh} />}
          {tab === "releases" && <ReleaseHistory releases={releases} />}
          {tab === "sessions" && <div className="overflow-auto"><table className="w-full text-left text-xs">
            <thead><tr className="border-b">{["主 Session", "业务发生时间", "拉取时间", "归属用户", "状态"].map(h =>
              <th key={h} className="p-3 font-medium">{h}</th>)}</tr></thead>
            <tbody>{detail.sessions?.map(session => <tr key={session.session_id} className="border-b">
              <td className="p-3 font-mono">{session.session_id}</td><td className="p-3">{maTime(session.record.business_time)}</td>
              <td className="p-3">{maTime(session.record.last_pulled_at)}</td><td className="p-3">{session.record.user_id || "—"}</td>
              <td className="p-3">{session.record.status}</td></tr>)}</tbody></table></div>}
        </div>}
      </div>}
    {form && <ConnectionForm connection={form === "edit" ? detail || undefined : undefined} onClose={() => setForm(null)}
      onSaved={job => { setForm(null); void refresh(); if (job) void track(job); }} />}
  </div>;
}
