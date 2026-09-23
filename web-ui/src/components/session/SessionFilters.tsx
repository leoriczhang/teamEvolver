import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
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

/** Quick time-range presets; value is a short key stored in the filter. */
const TIME_PRESETS: { label: string; value: string; days: number }[] = [
  { label: "全部时间", value: "", days: 0 },
  { label: "今天", value: "today", days: 0 },
  { label: "最近 3 天", value: "3d", days: 3 },
  { label: "最近 7 天", value: "7d", days: 7 },
  { label: "最近 30 天", value: "30d", days: 30 },
  { label: "最近 90 天", value: "90d", days: 90 },
];

/** Convert a preset value to {start, end} date strings (YYYY-MM-DD). */
function presetToRange(value: string): { start?: string; end?: string } {
  if (!value) return {};
  const today = new Date();
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  if (value === "today") return { start: fmt(today), end: fmt(today) };
  const preset = TIME_PRESETS.find((p) => p.value === value);
  if (!preset) return {};
  const start = new Date(today);
  start.setDate(start.getDate() - (preset.days - 1));
  return { start: fmt(start), end: fmt(today) };
}

/** Derive the preset key from current start/end values (for dropdown sync). */
function rangeToPreset(start?: string, end?: string): string {
  if (!start && !end) return "";
  const today = new Date();
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const todayStr = fmt(today);
  if (start === todayStr && end === todayStr) return "today";
  for (const p of TIME_PRESETS) {
    if (p.days <= 1) continue;
    const s = new Date(today);
    s.setDate(s.getDate() - (p.days - 1));
    if (start === fmt(s) && end === todayStr) return p.value;
  }
  return "custom";
}

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
  const [showCustomTime, setShowCustomTime] = useState(false);
  // Keep the controls in sync when filters are reset / preset externally (URL).
  useEffect(() => {
    setDraft(value);
  }, [value]);

  const select =
    "h-8 max-w-[240px] shrink-0 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none";
  const dateInput =
    "h-8 shrink-0 rounded-lg border border-border bg-background px-2 text-xs outline-none";

  // ---- skill combobox state (searchable dropdown) ----
  // The filter row scrolls horizontally, so the dropdown is portalled to
  // <body> and positioned from the trigger rect to avoid being clipped.
  const [skillOpen, setSkillOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState("");
  const [skillMenuPos, setSkillMenuPos] = useState<{ top: number; left: number } | null>(null);
  const skillBtnRef = useRef<HTMLButtonElement>(null);
  const skillMenuRef = useRef<HTMLDivElement>(null);

  const closeSkillMenu = () => {
    setSkillOpen(false);
    setSkillMenuPos(null);
  };

  const toggleSkillMenu = () => {
    if (skillOpen) {
      closeSkillMenu();
      return;
    }
    const rect = skillBtnRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = 240;
    const height = 300;
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 12));
    const below = rect.bottom + height + 12 <= window.innerHeight;
    const top = below ? rect.bottom + 4 : Math.max(8, rect.top - height - 4);
    setSkillMenuPos({ top, left });
    setSkillOpen(true);
  };

  useEffect(() => {
    if (!skillOpen) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      if (skillMenuRef.current?.contains(target) || skillBtnRef.current?.contains(target)) return;
      closeSkillMenu();
    };
    // Repositioning on layout shifts is more jarring than closing the menu.
    const onShift = (e: Event) => {
      if (skillMenuRef.current?.contains(e.target as Node)) return;
      closeSkillMenu();
    };
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("resize", onShift);
    window.addEventListener("scroll", onShift, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("resize", onShift);
      window.removeEventListener("scroll", onShift, true);
    };
  }, [skillOpen]);
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
      className="inline-flex shrink-0 items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
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
    <div className="mb-3 px-[14px] pt-3">
      <div className="flex items-center gap-2">
        {/* Filters stay on a single row and scroll horizontally instead of wrapping. */}
        <div className="flex min-w-0 flex-1 flex-nowrap items-center gap-2 overflow-x-auto pb-1 [scrollbar-width:thin]">
          <input
            value={draft.search || ""}
            placeholder="搜索会话 / ID / 提交人"
            className="h-8 w-[180px] shrink-0 rounded-lg border border-border bg-background px-2.5 text-xs outline-none"
            onChange={(e) => setDraft({ ...draft, search: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && apply(draft)}
          />
        {showTime && (
          <>
            <select
              value={rangeToPreset(draft.start, draft.end)}
              title="时间范围"
              className={select}
              onChange={(e) => {
                const val = e.target.value;
                if (val === "custom") {
                  setShowCustomTime(true);
                  return;
                }
                const range = presetToRange(val);
                const next = { ...draft, start: range.start || "", end: range.end || "" };
                setDraft(next);
                apply(next);
              }}
            >
              {TIME_PRESETS.map((p) => (
                <option key={p.value || "all"} value={p.value}>{p.label}</option>
              ))}
              <option value="custom">自定义…</option>
            </select>
            {showCustomTime && (
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
                <span className="shrink-0 text-xs text-muted-foreground">~</span>
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
                <button
                  className="shrink-0 text-xs text-muted-foreground hover:text-foreground"
                  title="收起自定义日期"
                  onClick={() => setShowCustomTime(false)}
                >
                  收起
                </button>
              </>
            )}
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
          <>
            <button
              ref={skillBtnRef}
              className="inline-flex h-8 max-w-[200px] shrink-0 items-center gap-1 rounded-lg border border-border bg-background px-2 text-xs font-semibold hover:bg-muted"
              title="按会话使用的 skill 分组筛选"
              onClick={toggleSkillMenu}
            >
              <span className="truncate">{draft.skill || "全部 Skill"}</span>
              <span className="text-[9px] text-muted-foreground">▾</span>
            </button>
            {skillOpen &&
              skillMenuPos &&
              createPortal(
                <div
                  ref={skillMenuRef}
                  className="fixed z-50 w-[240px] rounded-lg border border-border bg-background p-2 shadow-lg"
                  style={{ top: skillMenuPos.top, left: skillMenuPos.left }}
                >
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
                        closeSkillMenu();
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
                          closeSkillMenu();
                        }}
                      >
                        {name} ({count})
                      </button>
                    ))}
                    {!skillMatches.length && (
                      <div className="px-2 py-1 text-xs text-muted-foreground">无匹配 skill</div>
                    )}
                  </div>
                </div>,
                document.body
              )}
          </>
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
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
        <button
          className="h-8 shrink-0 rounded-lg border border-border px-3 text-xs font-semibold hover:bg-muted"
          onClick={() => apply(draft)}
        >
          查询
        </button>
      </div>
      {hasActive && (
        <div className="mt-2 flex flex-nowrap items-center gap-1.5 overflow-x-auto pb-1 [scrollbar-width:thin]">
          <span className="shrink-0 text-[11px] text-muted-foreground">筛选条件：</span>
          {activeEntries.map(([k, v]) => {
            const shown =
              k === "search" ? String(v) : FILTER_LABELS[k] ? `${FILTER_LABELS[k]}: ${v}` : `${k}: ${v}`;
            return chip(k, shown);
          })}
          <button
            className="shrink-0 text-[11px] text-accent hover:underline"
            onClick={() => apply({})}
          >
            清除全部
          </button>
        </div>
      )}
    </div>
  );
}
