import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import { saveTargets, type MAResource } from "./api/client";

export default function ResourceBindings({ connectionId, resources, canManage, onSaved }: {
  connectionId: string; resources: MAResource[]; canManage: boolean; onSaved: () => void;
}) {
  const agents = resources.filter(r => r.kind === "agent");
  const [editing, setEditing] = useState<MAResource | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    if (!editing) return;
    setBusy(true); setError("");
    try {
      await saveTargets(connectionId, editing.logical_name, editing.revision, selected);
      setEditing(null); onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
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
        {["托管 Skill", "源／应用版本", "团队版本", "目标成员", "状态"].map(name =>
          <th key={name} className="p-3 font-medium">{name}</th>)}
      </tr></thead><tbody>{resources.filter(r => r.kind === "skill").map(skill =>
        <tr key={skill.logical_name} className="border-b">
          <td className="p-3"><span className="font-semibold">{skill.logical_name}</span>
            <div className="mt-1 font-mono text-muted-foreground">{skill.record.tree_sha256?.slice(0, 16)}</div></td>
          <td className="p-3">v{skill.record.source_version || "—"} → v{skill.record.version || "—"}</td>
          <td className="p-3">{skill.record.team_version ?? "待发布"}</td>
          <td className="p-3">{(skill.record.agents || []).map(id =>
            agents.find(a => a.logical_name === id)?.record.name || id).join("、") || "待关联"}
            {canManage && <Button className="ml-2" size="sm" variant="ghost" onClick={() => {
              setEditing(skill); setSelected(skill.record.agents || []); setError("");
            }}>编辑关联</Button>}</td>
          <td className="p-3"><Pill tone={skill.record.drift ? "red" : "green"}>
            {skill.record.drift || skill.record.status || "已托管"}</Pill></td>
        </tr>)}</tbody></table></div>
    {editing && <div className="rounded-lg border border-accent/30 bg-accent-soft p-4">
      <div className="text-sm font-semibold">{editing.logical_name} · 选择目标成员</div>
      <div className="my-3 flex flex-wrap gap-4">{agents.map(agent =>
        <label key={agent.logical_name} className="flex gap-2 text-xs"><input type="checkbox"
          checked={selected.includes(agent.logical_name)} onChange={e => setSelected(old => e.target.checked
            ? [...old, agent.logical_name] : old.filter(id => id !== agent.logical_name))} />
          {agent.record.name || agent.logical_name}</label>)}</div>
      <p className="mb-3 text-xs text-muted-foreground">关联变更需按新目标重新验证。已受理回放继续使用固定关联。</p>
      {error && <p role="alert" className="mb-2 text-xs text-destructive">{error}</p>}
      <Button size="sm" disabled={busy} onClick={save}>保存关联</Button>
      <Button className="ml-2" size="sm" variant="ghost" onClick={() => setEditing(null)}>取消</Button>
    </div>}
  </div>;
}
