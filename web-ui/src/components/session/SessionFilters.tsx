import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { SessionListFilters } from "@/hooks/useSessionList";

/** Sort options mapped to server-side sort_by/order pairs. */
const SORT_OPTIONS: { label: string; sortBy: string; order: string }[] = [
  { label: "最新优先", sortBy: "", order: "" },
  { label: "最旧优先", sortBy: "time", order: "asc" },
  { label: "分数最低（最差优先）", sortBy: "score", order: "asc" },
  { label: "分数最高", sortBy: "score", order: "desc" },
  { label: "轮数最多", sortBy: "turns", order: "desc" },
];

const FILTER_LABELS: Record<string, string> = {
  search: "搜索",
  status: "状态",
  decision: "判别",
  case: "结论",
  skill: "Skill",
  start: "开始",
  end: "结束",
  sort_by: "排序",
};

function sortValue(f: SessionListFilters): string {
  const opt = SORT_OPTIONS.find(
    (o) => (o.sortBy || "") === (f.sort_by || "") && (o.order || "") === (f.order || "")
  );
  return opt ? `${opt.sortBy}|${opt.order}` : "|";
}

/**
 * Shared filter bar for the unified session list. Select/date filters apply
 * immediately; the search box applies on Enter or via the 查询 button.
 * Active filters render as removable chips; `actions` (e.g. batch export)
 * renders to the left of the 查询 button.
 */
export default function SessionFilters({
  value,
  onApply,
  skillOptions,
  actions,
  showDecision = true,
  showCase = true,
  showSkill = true,
  showTime = true,
  showSort = true,
}: {
  value: SessionListFilters;
  onApply: (next: SessionListFilters) => void;
  /** [skill name, session count] pairs for the skill combobox. */
  skillOptions?: [string, number][];
  actions?: ReactNode;
  showDecision?: boolean;
  showCase?: boolean;
  showSkill?: boolean;
  showTime?: boolean;
  showSort?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  // Keep the controls in sync when filters are reset / preset externally (URL).
  useEffect(() => {
    setDraft(value);
  }, [value]);

  const select =
    "h-8 max-w-[240px] rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none";
  const dateInput =
    "h-8 rounded-lg border border-border bg-background px-2 text-xs outline-none";

  // ---- skill combobox state (searchable dropdown) ----
  const [skillOpen, setSkillOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState("");
  const skillRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (skillRef.current && !skillRef.current.contains(e.target as Node)) {
        setSkillOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  const skillMatches = useMemo(() => {
    const q = skillQuery.trim().toLowerCase();
    const all = skillOptions || [];
    const hit = q ? all.filter(([n]) => n.toLowerCase().includes(q)) : all;
    return hit.slice(0, 50);
  }, [skillOptions, skillQuery]);

  const apply = (next: SessionListFilters) => onApply(next);
  // Sorting is not a "filter" — exclude it from the active chips.
  const activeEntries = Object.entries(value).filter(
    ([k, v]) => v && k !== "order" && k !== "sort_by"
  );
  const hasActive = activeEntries.length > 0;
  const removeFilter = (key: string) => {
    const next: SessionListFilters = { ...value };
    delete next[key as keyof SessionListFilters];
    apply(next);
  };

  const chip = (key: string, label: string) => (
    <span
      key={key}
      className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
    >
      {label}
      <button
        className="text-muted-foreground hover:text-foreground"
        title="移除该筛选"
        onClick={() => removeFilter(key)}
      >
        ×
      </button>
    </span>
  );

  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <input
          value={draft.search || ""}
          placeholder="搜索会话 / ID / 提交人"
          className="h-8 w-[180px] rounded-lg border border-border bg-background px-2.5 text-xs outline-none"
          onChange={(e) => setDraft({ ...draft, search: e.target.value })}
          onKeyDown={(e) => e.key === "Enter" && apply(draft)}
        />
        {showTime && (
          <>
            <input
              type="date"
              value={draft.start || ""}
              title="开始日期（按入库时间）"
              className={dateInput}
              onChange={(e) => {
                const next = { ...draft, start: e.target.value };
                setDraft(next);
                apply(next);
              }}
            />
            <span className="text-xs text-muted-foreground">~</span>
            <input
              type="date"
              value={draft.end || ""}
              title="结束日期（含当天）"
              className={dateInput}
              onChange={(e) => {
                const next = { ...draft, end: e.target.value };
                setDraft(next);
                apply(next);
              }}
            />
          </>
        )}
        <select
          value={draft.status || ""}
          onChange={(e) => {
            const next = { ...draft, status: e.target.value };
            setDraft(next);
            apply(next);
          }}
          className={select}
        >
          <option value="">全部状态</option>
          <option value="queued">排队中</option>
          <option value="consumed">已消费</option>
          <option value="skipped">已跳过</option>
        </select>
        {showDecision && (
          <select
            value={draft.decision || ""}
            onChange={(e) => {
              const next = { ...draft, decision: e.target.value };
              setDraft(next);
              apply(next);
            }}
            className={select}
          >
            <option value="">全部判别</option>
            <option value="valuable">有价值</option>
            <option value="chitchat">闲聊</option>
          </select>
        )}
        {showCase && (
          <select
            value={draft.case || ""}
            onChange={(e) => {
              const next = { ...draft, case: e.target.value };
              setDraft(next);
              apply(next);
            }}
            className={select}
          >
            <option value="">全部结论</option>
            <option value="good">Good Case</option>
            <option value="bad">Bad Case</option>
          </select>
        )}
        {showSkill && (
          <div ref={skillRef} className="relative">
            <button
              className="h-8 max-w-[200px] truncate rounded-lg border border-border bg-background px-2 text-xs font-semibold hover:bg-muted"
              title="按会话使用的 skill 分组筛选"
              onClick={() => setSkillOpen((o) => !o)}
            >
              {draft.skill || "全部 Skill"} ▾
            </button>
            {skillOpen && (
              <div className="absolute right-0 z-20 mt-1 w-[240px] rounded-lg border border-border bg-background p-2 shadow-lg">
                <input
                  autoFocus
                  value={skillQuery}
                  placeholder="搜索 skill…"
                  className="mb-1.5 h-7 w-full rounded border border-border px-2 text-xs outline-none"
                  onChange={(e) => setSkillQuery(e.target.value)}
                />
                <div className="max-h-[240px] overflow-auto">
                  <button
                    className="block w-full rounded px-2 py-1 text-left text-xs hover:bg-muted"
                    onClick={() => {
                      const next = { ...draft, skill: "" };
                      setDraft(next);
                      apply(next);
                      setSkillOpen(false);
                    }}
                  >
                    全部 Skill
                  </button>
                  {skillMatches.map(([name, count]) => (
                    <button
                      key={name}
                      className={`block w-full truncate rounded px-2 py-1 text-left text-xs hover:bg-muted ${
                        draft.skill === name ? "font-semibold text-accent" : ""
                      }`}
                      title={`${name}（${count} 个会话）`}
                      onClick={() => {
                        const next = { ...draft, skill: name };
                        setDraft(next);
                        apply(next);
                        setSkillOpen(false);
                      }}
                    >
                      {name} ({count})
                    </button>
                  ))}
                  {!skillMatches.length && (
                    <div className="px-2 py-1 text-xs text-muted-foreground">无匹配 skill</div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
        {showSort && (
          <select
            value={sortValue(draft)}
            onChange={(e) => {
              const [sortBy, order] = e.target.value.split("|");
              const next = { ...draft, sort_by: sortBy, order };
              setDraft(next);
              apply(next);
            }}
            className={select}
            title="列表排序方式"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.label} value={`${o.sortBy}|${o.order}`}>
                {o.label}
              </option>
            ))}
          </select>
        )}
        {actions}
        <button
          className="h-8 rounded-lg border border-border px-3 text-xs font-semibold hover:bg-muted"
          onClick={() => apply(draft)}
        >
          查询
        </button>
      </div>
      {hasActive && (
        <div className="mt-2 flex flex-wrap items-center justify-end gap-1.5">
          <span className="text-[11px] text-muted-foreground">筛选条件：</span>
          {activeEntries.map(([k, v]) => {
            const shown =
              k === "search" ? String(v) : FILTER_LABELS[k] ? `${FILTER_LABELS[k]}: ${v}` : `${k}: ${v}`;
            return chip(k, shown);
          })}
          <button
            className="text-[11px] text-accent hover:underline"
            onClick={() => apply({})}
          >
            清除全部
          </button>
        </div>
      )}
    </div>
  );
}
