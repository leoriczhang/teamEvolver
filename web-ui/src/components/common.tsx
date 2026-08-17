import * as React from "react";
import { cn } from "@/lib/utils";
import { fmtScore, scoreClass } from "@/lib/format";

export const DEFAULT_PAGE_SIZE = 50;
export const MAX_VISIBLE_PAGES = 50;

// ---- Status dot ---------------------------------------------------------- //
export type DotState = "on" | "off" | "err" | "run";
export function Dot({ state, className }: { state: DotState; className?: string }) {
  return <span className={cn("dot", state, className)} />;
}

// ---- Soft coloured pill (badge look from console.html) ------------------- //
export type PillTone = "green" | "amber" | "red" | "blue" | "purple" | "gray";
export function Pill({
  tone = "gray",
  children,
  className,
}: {
  tone?: PillTone;
  children: React.ReactNode;
  className?: string;
}) {
  return <span className={cn("pill", tone, className)}>{children}</span>;
}

export function UserBadge({ name }: { name?: string | null }) {
  return <Pill tone="purple">{name || "unknown"}</Pill>;
}

// ---- Consistent page identity header ----------------------------------- //
export function PageHeader({
  title,
  description,
  badge,
  actions,
}: {
  title: React.ReactNode;
  description: React.ReactNode;
  badge?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="min-w-0">
        <h1 className="flex flex-wrap items-center gap-2 text-[20px] font-[800] leading-tight">
          {title}
          {badge && <Pill tone="purple">{badge}</Pill>}
        </h1>
        <p className="mt-1 max-w-[840px] text-[12px] leading-relaxed text-[#626b80]">{description}</p>
      </div>
      {actions && <div className="shrink-0">{actions}</div>}
    </header>
  );
}

// ---- Score text ---------------------------------------------------------- //
export function ScoreText({
  value,
  threshold,
  pending,
}: {
  value?: number | null;
  threshold?: number | null;
  pending?: string;
}) {
  if (value == null || isNaN(Number(value))) {
    return <span className="score pending">{pending || "—"}</span>;
  }
  return (
    <span className={cn("score", scoreClass(value, threshold))}>
      {fmtScore(value)}
    </span>
  );
}

// ---- Stat card ----------------------------------------------------------- //
export function StatCard({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="min-h-[76px] rounded-[14px] border border-border bg-white p-[14px] shadow-[var(--shadow-soft)] transition-[border-color,box-shadow,transform] hover:-translate-y-px hover:border-border-strong hover:shadow-[0_14px_34px_rgba(15,23,42,0.075)]">
      <div className="mb-1.5 text-[11px] font-[700] text-muted-foreground">{label}</div>
      <div
        className={cn(
          "text-[22px] font-[800] leading-tight",
          mono && "mono break-all text-[11px] font-[500] leading-relaxed text-[#464c5e]"
        )}
      >
        {value}
      </div>
    </div>
  );
}

// ---- Panel (section with header) ----------------------------------------- //
export function Panel({
  title,
  count,
  extra,
  children,
}: {
  title: React.ReactNode;
  count?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="ui-panel overflow-hidden rounded-[14px] border border-border bg-surface shadow-[var(--shadow-soft)]">
      <div className="flex min-h-[46px] items-center justify-between gap-3 border-b border-line bg-surface-subtle px-[14px] py-[10px]">
        <h2 className="flex items-center gap-2 text-[13px] font-[780]">
          <span>{title}</span>
          {count != null && (
            <span className="text-xs font-normal text-muted-foreground">{count}</span>
          )}
        </h2>
        {extra}
      </div>
      {children}
    </section>
  );
}

// ---- Paginated list helpers --------------------------------------------- //
export function usePagedItems<T>(
  items: T[],
  pageSize: number = DEFAULT_PAGE_SIZE,
  maxVisiblePages: number = MAX_VISIBLE_PAGES
) {
  const safePageSize = Math.max(1, Number(pageSize || DEFAULT_PAGE_SIZE));
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / safePageSize));
  const [page, setPage] = React.useState(1);

  React.useEffect(() => {
    setPage((p) => Math.min(Math.max(1, p), totalPages));
  }, [totalPages]);

  const currentPage = Math.min(Math.max(1, page), totalPages);
  const start = (currentPage - 1) * safePageSize;
  const end = Math.min(total, start + safePageSize);
  const pageItems = React.useMemo(() => items.slice(start, end), [items, start, end]);
  const visiblePages = Math.min(totalPages, Math.max(1, maxVisiblePages));

  return {
    items: pageItems,
    page: currentPage,
    setPage,
    pageSize: safePageSize,
    total,
    totalPages,
    visiblePages,
    start,
    end,
    hasPagination: total > safePageSize,
  };
}

export function ListViewport({
  children,
  maxHeight = "520px",
}: {
  children: React.ReactNode;
  maxHeight?: string;
}) {
  return (
    <div className="overflow-auto" style={{ maxHeight }}>
      {children}
    </div>
  );
}

export function PaginationControls({
  page,
  totalPages,
  visiblePages = MAX_VISIBLE_PAGES,
  total,
  start,
  end,
  onPageChange,
}: {
  page: number;
  totalPages: number;
  visiblePages?: number;
  total: number;
  start: number;
  end: number;
  onPageChange: (page: number) => void;
}) {
  if (totalPages <= 1) {
    return null;
  }
  const capped = Math.min(totalPages, Math.max(1, visiblePages));
  const maxButtons = Math.min(7, capped);
  const candidates = new Set<number>([1, totalPages, page]);
  for (let offset = 1; candidates.size < maxButtons && offset < totalPages; offset += 1) {
    if (page - offset > 1) candidates.add(page - offset);
    if (candidates.size < maxButtons && page + offset < totalPages) candidates.add(page + offset);
  }
  for (let edge = 2; candidates.size < maxButtons && edge < totalPages; edge += 1) {
    candidates.add(edge);
  }
  const pages = [...candidates].sort((a, b) => a - b);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line bg-surface-subtle px-[14px] py-[10px] text-[11px] text-muted-foreground">
      <div>
        显示 {start + 1}-{end} / {total}，第 {page} / {totalPages} 页
      </div>
      <div className="flex max-w-full flex-wrap items-center gap-1">
        <button
          className="min-w-7 rounded-lg border border-border bg-surface px-2 py-1.5 font-semibold hover:border-border-strong hover:bg-muted disabled:opacity-40"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          上一页
        </button>
        {pages.map((p, index) => (
          <React.Fragment key={p}>
            {index > 0 && p - pages[index - 1] > 1 && <span className="px-1 text-muted-soft">…</span>}
            <button
              className={cn(
                "min-w-7 rounded-lg border px-2 py-1.5 font-semibold",
                p === page
                  ? "border-sidebar-primary bg-sidebar-primary text-white shadow-sm"
                  : "border-border bg-surface hover:border-border-strong hover:bg-muted"
              )}
              onClick={() => onPageChange(p)}
            >
              {p}
            </button>
          </React.Fragment>
        ))}
        <button
          className="min-w-7 rounded-lg border border-border bg-surface px-2 py-1.5 font-semibold hover:border-border-strong hover:bg-muted disabled:opacity-40"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}

// ---- Empty / error rows -------------------------------------------------- //
export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[160px] place-items-center px-4 py-8 text-center text-sm text-muted-foreground">{children}</div>;
}
export function ErrorText({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[120px] place-items-center px-4 py-8 text-center text-sm text-destructive">{children}</div>;
}
