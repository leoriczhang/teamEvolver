import { Pill } from "@/components/common";
import type { LedgerRow,SessionListSkillVersion } from "@/features/sessions/api/client";

function entries(names: string[], versions: SessionListSkillVersion[]) {
  const rows = [...versions, ...names.filter(name => !versions.some(row => row.name === name))
    .map(name => ({ name, version: null } as SessionListSkillVersion))];
  const grouped = new Map<string, SessionListSkillVersion[]>();
  for (const row of rows) {
    const key = JSON.stringify([row.name, row.version]);
    grouped.set(key, [...(grouped.get(key) || []), row]);
  }
  return [...grouped.entries()];
}

function Chips({ items, used }: { items: ReturnType<typeof entries>; used: boolean }) {
  const chip = ([key, rows]: (typeof items)[number]) => {
    const { name, version } = rows[0];
    const agents = [...new Set(rows.map(row => row.agent_name).filter(Boolean))];
    const reason = rows.find(row => row.unknown_reason)?.unknown_reason;
    return (
      <span key={key} className="max-w-full break-all" title={[
        agents.length ? `Agent：${agents.join("、")}` : "",
        reason || "",
      ].filter(Boolean).join("\n") || undefined}>
        <Pill tone={used ? "blue" : "gray"} className="max-w-full">
          <span className="whitespace-normal break-all">{name} · {version ? `v${version}` : "版本未知"}</span>
        </Pill>
      </span>
    );
  };
  return (
    <>
      <div className="flex flex-wrap gap-1">{items.slice(0, 3).map(chip)}</div>
      {items.length > 3 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-[11px] text-accent">展开其余 {items.length - 3} 项</summary>
          <div className="mt-1 flex flex-wrap gap-1">{items.slice(3).map(chip)}</div>
        </details>
      )}
    </>
  );
}

export default function SessionSkillChips({ row }: { row: LedgerRow }) {
  const used = entries(row.used_skills || [], row.used_skill_versions || []);
  const all = entries(row.available_skills || [], row.available_skill_versions || []);
  const usedKeys = new Set(used.map(([key]) => key));
  const available = all.filter(([key]) => !usedKeys.has(key));
  const usedCount = new Set(used.map(([, rows]) => rows[0].name)).size;
  const availableCount = new Set(all.map(([, rows]) => rows[0].name)).size;
  return (
    <div className="w-[280px] min-w-[280px] space-y-2">
      <div className="whitespace-nowrap text-[11px] text-muted-foreground"
        title="实际使用按主、子 Agent 调用证据统计；版本来自会话归档快照。">
        实际使用 {usedCount}
        {row.available_skills != null && ` / 可用 ${availableCount}`}
      </div>
      {used.length > 0 && <Chips items={used} used />}
      {available.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] text-muted-foreground">可用（未确认调用）</div>
          <Chips items={available} used={false} />
        </div>
      )}
      {!used.length && !available.length && <span className="text-xs text-muted-foreground">无 Skill 记录</span>}
    </div>
  );
}
