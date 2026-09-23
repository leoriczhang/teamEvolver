import { useCallback, useEffect, useRef, useState } from "react";
import { api, type LedgerRow, type PageResponse } from "@/api/client";

export interface SessionListFilters {
  search?: string;
  status?: string;
  decision?: string;
  case?: string;
  skill?: string;
  /** Ingest-time bounds; date-only strings cover the whole local day. */
  start?: string;
  end?: string;
  /** Server-side sort: time | score | turns (+ order asc/desc). */
  sort_by?: string;
  order?: string;
}

export interface UseSessionListOptions {
  /** Server-side filters; identity of individual fields matters, not the object. */
  filters?: SessionListFilters;
  pageSize?: number;
  /** Poll interval in ms; 0 disables polling (default). */
  pollMs?: number;
  /** Fetch only when true (default). Failures keep the last good data. */
  enabled?: boolean;
}

export interface SessionListState {
  rows: LedgerRow[] | null;
  total: number;
  page: number;
  setPage: (page: number) => void;
  loading: boolean;
  /** First load returned empty but is being confirmed once (cold-store guard). */
  verifying: boolean;
  error: string | null;
  /** Quality summary over the filtered set (good/bad/valuable/avg_score). */
  stats: SessionListStats | null;
  /** skill → session count within the current filter scope (top 100). */
  skillCounts: [string, number][];
  reload: () => void;
}

export interface SessionListStats {
  total: number;
  good: number;
  bad: number;
  valuable: number;
  chitchat: number;
  avg_score: number | null;
}

/**
 * Single data source for every session list in the console: the
 * `/conversations` ledger (archive covers queued / consumed / skipped).
 * Server-side filters (search / status / decision / case) + server paging.
 */
export function useSessionList({
  filters = {},
  pageSize = 20,
  pollMs = 0,
  enabled = true,
}: UseSessionListOptions = {}): SessionListState {
  const [rows, setRows] = useState<LedgerRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<SessionListStats | null>(null);
  const [skillCounts, setSkillCounts] = useState<[string, number][]>([]);
  const [reloadTick, setReloadTick] = useState(0);
  // True while a first-load empty result is being double-checked (cold tenant
  // store); the panel stays in its loading state during this window.
  const [verifying, setVerifying] = useState(false);
  const inflight = useRef(false);
  const hasRows = useRef(false);
  // A just-switched tenant's store can answer 200 with zero rows while its
  // index/pool is still warming up. Don't commit that transient empty on the
  // very first load — keep the panel in loading state and confirm once after
  // a short delay, so the page never flashes "0 条/暂无会话" then reflows.
  const expectEmptyConfirm = useRef(false);
  const scopeCommitted = useRef(false);
  const emptyCheckTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { search, status, decision, case: caseFilter, skill, start, end, sort_by, order } = filters;
  const filterScope = [search, status, decision, caseFilter, skill, start, end, sort_by, order]
    .map((v) => String(v ?? ""))
    .join("|");
  const hasNoFilters = !search && !status && !decision && !caseFilter && !skill && !start && !end;

  // Any filter / scope change restarts the empty-confirmation dance.
  useEffect(() => {
    expectEmptyConfirm.current = false;
    scopeCommitted.current = false;
    setVerifying(false);
    if (emptyCheckTimer.current) {
      clearTimeout(emptyCheckTimer.current);
      emptyCheckTimer.current = null;
    }
  }, [filterScope]);

  const load = useCallback(
    async (force: boolean, currentPage: number) => {
      if (!enabled) return;
      if (inflight.current && !force) return;
      inflight.current = true;
      setLoading(true);
      try {
        const qs = new URLSearchParams();
        qs.set("limit", String(pageSize));
        qs.set("offset", String((Math.max(1, currentPage) - 1) * pageSize));
        if (force) qs.set("refresh", "true");
        if (search) qs.set("search", search);
        if (status) qs.set("status", status);
        if (decision) qs.set("decision", decision);
        if (caseFilter) qs.set("case", caseFilter);
        if (skill) qs.set("skill", skill);
        if (start) qs.set("start", start);
        if (end) qs.set("end", end);
        if (sort_by) qs.set("sort_by", sort_by);
        if (order) qs.set("order", order);
        const data = await api<
          PageResponse<LedgerRow> & {
            skill_counts?: Record<string, number>;
            stats?: SessionListStats;
            reachable?: boolean;
            reason?: string;
          }
        >(`/conversations?${qs.toString()}`);
        // The ledger endpoint answers HTTP 200 with reachable:false when the
        // tenant store is cold/unavailable (e.g. right after a tenant switch
        // while the PG pool warms up). Treat it as a failure so we keep the
        // previous list (or stay in loading) instead of flashing an empty
        // "0 条 / 暂无会话" page that suddenly reflows when data arrives.
        if (data.reachable === false) {
          throw new Error(data.reason || "会话存储暂时不可用，正在重试…");
        }
        const list = data.conversations || [];
        const unfilteredFirstPage = currentPage === 1 && hasNoFilters;
        if (list.length === 0 && unfilteredFirstPage && hasRows.current) {
          // An empty response while a non-empty list is on screen is treated
          // as suspicious (transient backend stall answers 200 with zero items).
          return;
        }
        if (list.length === 0 && unfilteredFirstPage && !scopeCommitted.current) {
          // First load of this scope came back empty. That may be real, but
          // a cold tenant store (right after switching) can also return zero
          // while warming up — confirm once after a short delay instead of
          // flashing an empty panel that reflows when rows arrive.
          const clearPendingCheck = () => {
            if (emptyCheckTimer.current) {
              clearTimeout(emptyCheckTimer.current);
              emptyCheckTimer.current = null;
            }
          };
          if (!expectEmptyConfirm.current) {
            expectEmptyConfirm.current = true;
            setVerifying(true);
            clearPendingCheck();
            emptyCheckTimer.current = setTimeout(() => {
              emptyCheckTimer.current = null;
              setReloadTick((t) => t + 1);
            }, 2500);
            return;
          }
          // Second (confirming) load is still empty — accept it.
          expectEmptyConfirm.current = false;
          setVerifying(false);
          clearPendingCheck();
        } else if (list.length > 0) {
          expectEmptyConfirm.current = false;
          setVerifying(false);
          if (emptyCheckTimer.current) {
            clearTimeout(emptyCheckTimer.current);
            emptyCheckTimer.current = null;
          }
        }
        scopeCommitted.current = true;
        hasRows.current = list.length > 0;
        setRows(list);
        setTotal(data.total ?? list.length);
        setStats(data.stats ?? null);
        setSkillCounts(Object.entries(data.skill_counts || {}));
        setError(null);
      } catch (e: any) {
        setError(e?.message || String(e));
      } finally {
        inflight.current = false;
        setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enabled, pageSize, search, status, decision, caseFilter, skill, start, end, sort_by, order]
  );

  // Reset to first page whenever filters change.
  useEffect(() => {
    setPage(1);
  }, [search, status, decision, caseFilter, skill, start, end, sort_by, order]);

  useEffect(() => {
    if (!enabled) return;
    void load(true, page);
    if (!pollMs) return;
    const id = setInterval(() => void load(false, page), pollMs);
    return () => clearInterval(id);
  }, [enabled, load, page, pollMs, reloadTick]);

  // Fast recovery for the cold-tenant window: while nothing is on screen and
  // the last load failed (store warming up / transient stall), retry sooner
  // than the full poll interval instead of leaving the user staring at an
  // empty panel for ~15s+.
  useEffect(() => {
    if (!enabled || !error || rows) return;
    const id = setTimeout(() => setReloadTick((t) => t + 1), 3000);
    return () => clearTimeout(id);
  }, [enabled, error, rows, reloadTick]);

  useEffect(
    () => () => {
      if (emptyCheckTimer.current) clearTimeout(emptyCheckTimer.current);
    },
    []
  );

  const reload = useCallback(() => setReloadTick((t) => t + 1), []);

  return {
    rows,
    total,
    page,
    setPage,
    loading: loading || verifying,
    verifying,
    error,
    stats,
    skillCounts,
    reload,
  };
}
