import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useEffect, useState } from "react";
import { maConnections, maResources, saveTargets, type MAConnection, type MAResource, type MATarget } from "./api/client";

export default function CandidateTargets({ skillName, canManage, matrix = {} }: {
  skillName: string; canManage: boolean; matrix?: Record<string, MATarget>;
}) {
  const [targets, setTargets] = useState<{ connection: MAConnection; resources: MAResource[] }[]>([]);
  const [chosen, setChosen] = useState<Record<string, string[]>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const connections = await maConnections();
        const items = [];
        for (const connection of connections.connections) {
          const { resources } = await maResources(connection.connection_id);
          items.push({ connection, resources });
        }
        if (!cancelled) {
          setTargets(items);
          setChosen(Object.fromEntries(items.map(({ connection, resources }) => [
            connection.connection_id, resources.find(r => r.kind === "skill" && r.logical_name === skillName)?.record.agents || [],
          ])));
        }
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : "加载 MA 目标失败"); }
    }
    void load();
    return () => { cancelled = true; };
  }, [skillName]);
  async function save(connection: MAConnection, resources: MAResource[]) {
    setBusy(connection.connection_id); setError("");
    try {
      const existing = resources.find(r => r.kind === "skill" && r.logical_name === skillName);
      await saveTargets(connection.connection_id, skillName, existing?.revision || 0, chosen[connection.connection_id] || []);
      const updated = await maResources(connection.connection_id);
      setTargets(old => old.map(item => item.connection.connection_id === connection.connection_id
        ? { ...item, resources: updated.resources } : item));
    } catch (e) { setError(e instanceof Error ? e.message : "关联保存失败"); }
    finally { setBusy(""); }
  }
  if (!targets.length && !error && !Object.keys(matrix).length) return null;
  return <section aria-label="MA 受管目标" className="rounded-lg border p-4 text-xs">
    <h3 className="mb-2 font-semibold text-sm">MA 受管目标与独立验证</h3>
    <p className="mb-3 text-muted-foreground">新 Skill 先选择成员并保存，再重新评估。每个受管连接均需验证通过。</p>
    {error && <p role="alert" className="mb-2 text-destructive">{error}</p>}
    {targets.map(({ connection, resources }) => {
      const id = connection.connection_id;
      return <div key={id} className="mb-3 rounded border p-3">
        <div className="flex justify-between"><strong>{connection.config.display_name}</strong>
          <Pill tone={matrix[id]?.accepted ? "green" : "amber"}>{matrix[id]?.accepted ? "目标已通过"
            : ({ missing: "缺少独立验证", inconclusive: "验证无法确定", rejected: "验证未通过",
                pending: "待验证", blocked: "验证受阻" } as Record<string, string>)[matrix[id]?.status || ""] || "待验证"}</Pill></div>
        <div className="my-3 flex flex-wrap gap-3">{resources.filter(r => r.kind === "agent").map(agent =>
          <label key={agent.logical_name} className="flex gap-1.5"><input type="checkbox" disabled={!canManage}
            checked={(chosen[id] || []).includes(agent.logical_name)} onChange={e => setChosen(old => ({
              ...old, [id]: e.target.checked ? [...old[id], agent.logical_name] : old[id].filter(v => v !== agent.logical_name),
            }))} />{agent.record.name || agent.logical_name}</label>)}</div>
        {canManage && <Button variant="outline" size="sm" disabled={!!busy} onClick={() => save(connection, resources)}>保存目标关联</Button>}
      </div>;
    })}
  </section>;
}
