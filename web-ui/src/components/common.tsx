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
  return <Pill tone="purple">👤 {name || "unknown"}</Pill>;
}

// ---- Consistent page identity header ----------------------------------- //
export function PageHeader({
  title,
  description,
  badge,
}: {
  title: React.ReactNode;
  description: React.ReactNode;
  badge?: React.ReactNode;
}) {
  return (
    <header className="border-b border-line bg-surface px-7 py-5">
      <h1 className="flex flex-wrap items-center gap-2.5 text-[22px] font-bold tracking-tight">
        {title}
        {badge && <Pill tone="purple">{badge}</Pill>}
      </h1>
      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{description}</p>
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
    <div className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-soft)]">
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{label}</div>
      <div
        className={cn(
          "text-2xl font-bold tracking-tight",
          mono && "mono text-xs break-all font-normal"
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
    <section className="mb-6 overflow-hidden rounded-lg border border-border bg-surface shadow-[var(--shadow-soft)]">
      <div className="flex items-center justify-between gap-2 border-b border-line bg-surface-subtle px-4 py-3">
        <h2 className="flex items-center gap-2 text-sm font-bold">
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
  const pages = Array.from({ length: capped }, (_, i) => i + 1);
  const canGoBeyondVisible = totalPages > capped;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line bg-surface-subtle px-4 py-3 text-xs text-muted-foreground">
      <div>
        显示 {start + 1}-{end} / {total}，第 {page} / {totalPages} 页
      </div>
      <div className="flex max-w-full flex-wrap items-center gap-1.5">
        <button
          className="rounded-md border border-border bg-surface px-2 py-1 font-semibold disabled:opacity-40"
          disabled={page <= 1}
          onClick={() => onPageChange(1)}
        >
          首页
        </button>
        <button
          className="rounded-md border border-border bg-surface px-2 py-1 font-semibold disabled:opacity-40"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          上一页
        </button>
        {pages.map((p) => (
          <button
            key={p}
            className={cn(
              "rounded-md border px-2 py-1 font-semibold",
              p === page
                ? "border-sidebar-primary bg-sidebar-primary text-white"
                : "border-border bg-surface hover:bg-muted"
            )}
            onClick={() => onPageChange(p)}
          >
            {p}
          </button>
        ))}
        {canGoBeyondVisible && (
          <span className="px-1 text-muted-soft">…</span>
        )}
        <button
          className="rounded-md border border-border bg-surface px-2 py-1 font-semibold disabled:opacity-40"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </button>
        <button
          className="rounded-md border border-border bg-surface px-2 py-1 font-semibold disabled:opacity-40"
          disabled={page >= totalPages}
          onClick={() => onPageChange(totalPages)}
        >
          末页
        </button>
      </div>
    </div>
  );
}

// ---- Empty / error rows -------------------------------------------------- //
export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-4 py-6 text-center text-sm text-muted-foreground">{children}</div>;
}
export function ErrorText({ children }: { children: React.ReactNode }) {
  return <div className="px-4 py-6 text-center text-sm text-destructive">{children}</div>;
}
