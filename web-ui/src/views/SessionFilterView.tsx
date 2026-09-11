import { useEffect, useRef, useState } from "react";
import { Panel, StatCard, PaginationControls } from "@/components/common";
import { api, type SessionFilterAuditResp } from "@/api/client";
import { useSessionList, type SessionListFilters } from "@/hooks/useSessionList";
import SessionTable from "@/components/session/SessionTable";
import SessionFiltersBar from "@/components/session/SessionFilters";
import { toastErr } from "@/lib/toast";

const PAGE_SIZE = 20;

export default function SessionFilterView({ active }: { active: boolean }) {
  const [stats, setStats] = useState<SessionFilterAuditResp["stats"] | null>(null);
  const [filters, setFilters] = useState<SessionListFilters>({});
  const loaded = useRef(false);

  const sessions = useSessionList({
    filters,
    pageSize: PAGE_SIZE,
    enabled: active,
  });

  // The audit stats come from the dedicated filter-audit endpoint; the session
  // list itself is the shared `/conversations` source (decision filter applies).
  useEffect(() => {
    if (!active || loaded.current) return;
    loaded.current = true;
    api<SessionFilterAuditResp>("/api/session-filter/audit?limit=1")
      .then((data) => setStats(data.stats || null))
      .catch((e: any) => toastErr("加载过滤统计失败", e.message));
  }, [active]);

  const valuable = stats?.decisions?.valuable || 0;
  const chitchat = stats?.decisions?.chitchat || 0;
  const modelMode = stats?.modes?.model || 0;
  const heuristicMode = stats?.modes?.heuristic || 0;

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="总判别数" value={stats?.total ?? "—"} />
        <StatCard label="进入进化" value={valuable} />
        <StatCard label="过滤闲聊" value={chitchat} />
        <StatCard label="模型 / 规则" value={`${modelMode} / ${heuristicMode}`} />
      </div>

      <Panel
        title="判别明细"
        count={sessions.rows ? `${sessions.total} 条` : ""}
      >
        <SessionFiltersBar value={filters} onApply={setFilters} showCase={false} />
        <SessionTable
          rows={sessions.rows}
          emptyText={sessions.error ? "加载失败：" + sessions.error : "暂无判别记录"}
        />
        <PaginationControls
          {...serverPager(sessions.page, sessions.total, sessions.rows?.length || 0)}
          onPageChange={sessions.setPage}
        />
      </Panel>
    </div>
  );
}

function serverPager(page: number, total: number, itemCount: number) {
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(Math.max(1, page), totalPages);
  const start = (currentPage - 1) * PAGE_SIZE;
  return {
    page: currentPage,
    totalPages,
    visiblePages: Math.min(10, totalPages),
    total,
    start,
    end: start + itemCount,
  };
}
