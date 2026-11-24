import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { bindingStatus } from "@/features/skills/bindingStatus";
import { useEffect, useRef, useState } from "react";
import { followMAJob, saveTargets, type MAResource } from "./api/client";

export default function ResourceBindings({ connectionId, resources, canManage, onSaved }: {
  connectionId: string; resources: MAResource[]; canManage: boolean; onSaved: () => void;
}) {
  const agents = resources.filter(r => r.kind === "agent");
  const [editing, setEditing] = useState<MAResource | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, [connectionId]);
  async function save() {
    if (!editing) return;
    const signal = lifetime.current!.signal;
    setBusy(true); setError("");
    try {
      const result = await saveTargets(connectionId, editing.logical_name, editing.revision, selected);
      if ("job_id" in result) await followMAJob(result, signal, () => {});
      if (signal.aborted) return;
      setEditing(null); onSaved();
    } catch (e) { if (!signal.aborted) setError(e instanceof Error ? e.message : "保存失败"); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  return <div className="space-y-4">
    <div className="grid gap-2 sm:grid-cols-3">{agents.map(agent =>
      <div key={agent.logical_name} className="rounded-lg border bg-surface-subtle p-3">
        <div className="font-semibold text-sm">{agent.record.name || agent.logical_name}</div>
        <div className="mt-1 truncate font-mono text-xs text-muted-foreground">{agent.logical_name}</div>
        <Pill tone="gray">Agent v{agent.record.version || "?"}</Pill>
      </div>)}</div>
    <div className="overflow-auto"><table className="w-full text-left text-xs">
      <thead><tr className="border-b text-muted-foreground">
        {["托管 Skill", "云端最新版本", "成员绑定版本", "已挂载成员", "状态"].map(name =>
          <th key={name} className="p-3 font-medium">{name}</th>)}
      </tr></thead><tbody>{resources.filter(r => r.kind === "skill").map(skill => {
        const state = bindingStatus({ ...skill.record, version: skill.record.latest_version });
        return <tr key={skill.logical_name} className="border-b">
          <td className="p-3"><span className="font-semibold">{skill.logical_name}</span>
            <div className="mt-1 font-mono text-muted-foreground">{skill.record.tree_sha256?.slice(0, 16)}</div></td>
          <td className="p-3">{skill.record.latest_version ? `v${skill.record.latest_version}` : "尚未发布"}</td>
          <td className="p-3">{state.versions}</td>
          <td className="p-3">{(skill.record.agents || []).map(id =>
            agents.find(a => a.logical_name === id)?.record.name || id).join("、") || "待关联"}
            {canManage && !skill.record.deleted && <Button className="ml-2" size="sm" variant="ghost" onClick={() => {
              setEditing(skill); setSelected(skill.record.desired_agents ?? skill.record.agents ?? []); setError("");
            }}>编辑关联</Button>}</td>
          <td className="p-3"><Pill tone={skill.record.drift ? "red" : skill.record.deleted ? "gray" : state.tone}>
            {skill.record.drift || (skill.record.deleted ? "已删除"
              : skill.record.latest_version ? state.label : "待发布")}</Pill></td>
        </tr>;
      })}</tbody></table></div>
    {editing && <div className="rounded-lg border border-accent/30 bg-accent-soft p-4">
      <div className="text-sm font-semibold">{editing.logical_name} · 选择目标成员</div>
      <div className="my-3 flex flex-wrap gap-4">{agents.map(agent =>
        <label key={agent.logical_name} className="flex gap-2 text-xs"><input type="checkbox" disabled={busy}
          checked={selected.includes(agent.logical_name)} onChange={e => setSelected(old => e.target.checked
            ? [...old, agent.logical_name] : old.filter(id => id !== agent.logical_name))} />
          {agent.record.name || agent.logical_name}</label>)}</div>
      <p className="mb-3 text-xs text-muted-foreground">保存后将云端最新版本挂载到所选成员，取消选择会卸载。成功回读后更新状态，Skill 版本不变。</p>
      {error && <p role="alert" className="mb-2 text-xs text-destructive">{error}</p>}
      <Button size="sm" disabled={busy} onClick={save}>{busy ? "正在应用关联…" : "保存关联"}</Button>
      <Button className="ml-2" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>取消</Button>
    </div>}
  </div>;
}
