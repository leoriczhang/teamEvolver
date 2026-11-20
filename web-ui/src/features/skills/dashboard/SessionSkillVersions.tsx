import { PaginationControls,Pill,usePagedItems } from "@/components/common";
import { type SessionSkillVersion } from "@/features/sessions/api/client";

export default function SessionSkillVersions({ rows }: { rows: SessionSkillVersion[] }) {
  const pager = usePagedItems(rows, 20);
  return (
    <section aria-label="Skill 版本" className="min-w-0 overflow-hidden rounded-lg border border-border bg-background/70">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5">
        <h3 className="text-xs font-semibold">Skill 版本</h3>
        <span className="text-[11px] text-muted-foreground">{rows.length} 条记录 · 会话归档快照</span>
      </div>
      {rows.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-surface-subtle text-[11px] text-muted-foreground">
              <tr>
                {["Skill", "版本", "Agent", "使用记录"].map(label => (
                  <th key={label} className="px-3 py-2 font-medium">{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pager.items.map((row, index) => (
                <tr key={`${row.agent_id || row.agent_name}:${row.skill_id || row.name}:${row.version}:${pager.start + index}`}
                  className="border-t border-border align-top">
                  <td className="max-w-[400px] px-3 py-2.5">
                    <span className="break-words font-mono font-medium" title={row.skill_id || undefined}>{row.name}</span>
                    {row.unknown_reason && <p className="mt-1 text-[11px] text-muted-foreground">{row.unknown_reason}</p>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <Pill tone={row.version ? "blue" : "amber"}>{row.version ? `v${row.version}` : "版本未知"}</Pill>
                  </td>
                  <td className="px-3 py-2.5">
                    <span>{row.agent_role === "main" ? "主 Agent" : row.agent_role === "member" ? "子 Agent" : "Agent 未确认"}</span>
                    <p className="mt-0.5 break-words text-[11px] text-muted-foreground">{row.agent_name}</p>
                    {row.agent_version && <p className="mt-0.5 text-[11px] text-muted-foreground">Agent v{row.agent_version}</p>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <Pill tone={row.usage === "used" ? "green" : "gray"}>
                      {row.usage === "used" ? "有调用记录" : "已挂载"}
                    </Pill>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="px-3 py-3 text-xs text-muted-foreground">该会话暂无 Skill 版本记录。</p>}
      <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
        版本来自本会话快照，调用状态来自归档证据；缺少固定版本或无法唯一匹配时显示未知。
      </div>
      <PaginationControls {...pager} onPageChange={pager.setPage} />
    </section>
  );
}
