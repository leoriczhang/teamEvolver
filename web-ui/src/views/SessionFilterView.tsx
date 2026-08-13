import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Empty,
  ListViewport,
  PaginationControls,
  Panel,
  Pill,
  StatCard,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  api,
  type SessionFilterAuditItem,
  type SessionFilterAuditResp,
} from "@/api/client";
import { fmtTime } from "@/lib/format";
import { toastErr } from "@/lib/toast";

type DecisionFilter = "" | "valuable" | "chitchat";

export default function SessionFilterView({ active }: { active: boolean }) {
  const [items, setItems] = useState<SessionFilterAuditItem[]>([]);
  const [stats, setStats] = useState<SessionFilterAuditResp["stats"] | null>(null);
  const [decision, setDecision] = useState<DecisionFilter>("");
  const [limit, setLimit] = useState(100);
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams();
      qs.set("limit", String(limit || 100));
      if (decision) qs.set("decision", decision);
      const data = await api<SessionFilterAuditResp>(`/api/session-filter/audit?${qs.toString()}`);
      setItems(data.items || []);
      setStats(data.stats || null);
    } catch (e: any) {
      toastErr("加载过滤审计失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [decision, limit]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  const valuable = stats?.decisions?.valuable || 0;
  const chitchat = stats?.decisions?.chitchat || 0;
  const modelMode = stats?.modes?.model || 0;
  const heuristicMode = stats?.modes?.heuristic || 0;
  const pager = usePagedItems(items);

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      <div className="mb-5 flex flex-wrap items-center justify-end gap-2">
        <select
            value={decision}
            onChange={(e) => setDecision(e.target.value as DecisionFilter)}
            className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
          >
            <option value="">全部判别</option>
            <option value="valuable">valuable</option>
            <option value="chitchat">chitchat</option>
        </select>
        <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
          >
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>最近 {n} 条</option>
            ))}
        </select>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          查询
        </Button>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="总判别数" value={stats?.total ?? "—"} />
        <StatCard label="进入进化" value={valuable} />
        <StatCard label="过滤闲聊" value={chitchat} />
        <StatCard label="模型 / 规则" value={`${modelMode} / ${heuristicMode}`} />
      </div>

      <Panel title="过滤明细" count={`${items.length} 条`}>
        {!items.length ? (
          <Empty>暂无过滤审计记录。</Empty>
        ) : (
          <>
            <ListViewport maxHeight="640px">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    {["判别", "会话", "提交人", "模式", "置信度", "原因", "时间"].map((h) => (
                      <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {pager.items.map((item) => {
                    const judge = item.value_judge || {};
                    return (
                      <tr key={item.session_id}>
                        <Td>
                          <Pill tone={judge.decision === "valuable" ? "green" : judge.decision === "chitchat" ? "gray" : "amber"}>
                            {judge.decision || "unknown"}
                          </Pill>
                        </Td>
                        <Td>
                          <div className="mono text-xs">{item.session_id}</div>
                          <div className="mt-1 max-w-[280px] truncate text-xs text-muted-foreground">
                            {item.title || "(无标题)"}
                          </div>
                        </Td>
                        <Td>{item.user_alias || "anonymous"}</Td>
                        <Td><Pill tone={judge.mode === "model" ? "blue" : "purple"}>{judge.mode || "unknown"}</Pill></Td>
                        <Td>{judge.confidence == null ? "—" : Number(judge.confidence).toFixed(2)}</Td>
                        <Td>
                          <span className="line-clamp-2 text-xs text-muted-foreground" title={judge.reason || ""}>
                            {judge.reason || "—"}
                          </span>
                        </Td>
                        <Td className="text-xs text-muted-foreground">
                          {fmtTime(item.recorded_at || item.ingested_at || item.timestamp)}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </ListViewport>
            <PaginationControls {...pager} onPageChange={pager.setPage} />
          </>
        )}
      </Panel>
    </div>
  );
}

function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`border-b border-line px-4 py-2.5 align-top text-sm ${className}`}>{children}</td>;
}
