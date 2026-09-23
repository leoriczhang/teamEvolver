import type { ReactNode } from "react";
import { UserBadge, Pill, Empty } from "@/components/common";
import { fmtTime } from "@/lib/format";
import type { LedgerRow } from "@/api/client";

export type SessTab = "detail" | "process";

export const GOOD_CASE_THRESHOLD = 0.6;

export function StatusBadge({ status }: { status?: string }) {
  if (status === "consumed") return <Pill tone="green">已消费</Pill>;
  if (status === "queued") return <Pill tone="amber">排队中</Pill>;
  if (status === "skipped") return <Pill tone="gray">已跳过</Pill>;
  return <Pill tone="gray">{status || "-"}</Pill>;
}

export function CaseBadge({ score }: { score?: number | null }) {
  if (score == null || typeof score !== "number") {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return score >= GOOD_CASE_THRESHOLD ? (
    <Pill tone="green">Good {score.toFixed(2)}</Pill>
  ) : (
    <Pill tone="red">Bad {score.toFixed(2)}</Pill>
  );
}

export function DecisionBadge({ value }: { value?: LedgerRow["value_judge"] }) {
  const decision = value?.decision;
  if (!decision) return <span className="text-xs text-muted-foreground">—</span>;
  const tone =
    decision === "valuable" ? "green" : decision === "chitchat" ? "gray" : "amber";
  const labels: Record<string, string> = { valuable: "有价值", chitchat: "闲聊" };
  const label = labels[decision] || decision;
  const title = [
    value?.reason ? "理由：" + value.reason : "",
    value?.confidence != null ? "置信度 " + Math.round(value.confidence * 100) + "%" : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span title={title || undefined}>
      <Pill tone={tone}>{label}</Pill>
    </span>
  );
}

const JUDGE_REASON_LABELS: Record<string, string> = {
  task_completion: "任务完成",
  response_quality: "回答质量",
  efficiency: "效率",
  tool_usage: "工具使用",
};

export function judgeTooltip(judge?: LedgerRow["judge"]): string | undefined {
  if (!judge) return undefined;
  const parts: string[] = [];
  if (judge.rationale) parts.push(judge.rationale);
  const reasons = judge.reasons || {};
  for (const [key, label] of Object.entries(JUDGE_REASON_LABELS)) {
    const items = reasons[key as keyof typeof reasons] || [];
    if (items.length) {
      parts.push(`${label}：\n- ${items.join("\n- ")}`);
    }
  }
  return parts.join("\n\n") || undefined;
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
      {children}
    </th>
  );
}

function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`border-b border-line px-4 py-2.5 align-top ${className}`}>{children}</td>;
}

/**
 * The single session list table used across the console (运行总览 / 过滤审计).
 * Title opens the session detail modal; the status badge opens the evolution
 * process view. Case/judge columns come from the `/conversations` enrichment.
 * Pass `selectedIds` + callbacks to enable checkbox batch selection.
 */
export default function SessionTable({
  rows,
  emptyText = "暂无会话",
  maxHeight,
  onOpen,
  selectedIds,
  onToggleSelect,
  onSelectAll,
}: {
  rows: LedgerRow[] | null;
  emptyText?: string;
  maxHeight?: string;
  onOpen?: (sid: string, tab: SessTab) => void;
  /** Controlled selection; selection UI appears when these are provided. */
  selectedIds?: string[];
  onToggleSelect?: (sid: string, checked: boolean) => void;
  onSelectAll?: (checked: boolean) => void;
}) {
  if (!rows?.length) return <Empty>{emptyText}</Empty>;
  const selectable = onToggleSelect != null && onSelectAll != null;
  const selected = new Set(selectedIds || []);
  const pageIds = rows.map((r) => r.session_id).filter(Boolean);
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const body = rows.map((r, i) => {
    const sid = r.session_id || "";
    const score = r.judge?.overall_score;
    return (
      <tr key={`${sid}-${i}`}>
        {selectable && (
          <Td>
            <input
              type="checkbox"
              checked={selected.has(sid)}
              onChange={(e) => onToggleSelect?.(sid, e.target.checked)}
            />
          </Td>
        )}
        <Td>
          <div
            className="link max-w-[360px] truncate text-accent"
            title={"点击查看会话内容：" + (r.title || sid)}
            onClick={onOpen ? () => onOpen(sid, "detail") : undefined}
          >
            {r.title || sid || "(无标题会话)"}
          </div>
          <div className="mono mt-1 max-w-[360px] truncate text-[11px] text-muted-foreground">
            {sid}
          </div>
        </Td>
        <Td>
          <UserBadge name={r.user_alias || "anonymous"} />
          {r.meta?.user_id && r.meta.user_id !== (r.user_alias || "") && (
            <div
              className="mono mt-1 max-w-[160px] truncate text-[11px] text-muted-foreground"
              title={"User ID：" + r.meta.user_id}
            >
              {r.meta.user_id}
            </div>
          )}
        </Td>
        <Td>{r.num_turns != null ? r.num_turns : "-"}</Td>
        <Td>
          {r.meta?.trace_id ? (
            <span
              className="mono block max-w-[200px] truncate text-[11px] text-muted-foreground"
              title={"Trace ID：" + r.meta.trace_id}
            >
              {r.meta.trace_id}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">—</span>
          )}
        </Td>
        <Td>
          <div className="flex max-w-[180px] flex-wrap gap-1">
            {(r.used_skills || []).length ? (
              r.used_skills!.map((name) => (
                <Pill key={name} tone="blue">
                  {name}
                </Pill>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">—</span>
            )}
          </div>
        </Td>
        <Td>
          <span
            className={onOpen ? "link inline-flex" : "inline-flex"}
            title={onOpen ? "点击查看进化过程明细" : undefined}
            onClick={onOpen ? () => onOpen(sid, "process") : undefined}
          >
            <StatusBadge status={r.status} />
          </span>
        </Td>
        <Td>
          <span
            className={onOpen ? "inline-flex cursor-pointer" : "inline-flex"}
            onClick={onOpen ? () => onOpen(sid, "detail") : undefined}
          >
            <DecisionBadge value={r.value_judge} />
          </span>
        </Td>
        <Td>
          <span
            title={judgeTooltip(r.judge) || (onOpen ? "点击查看评审详情" : undefined)}
            className={
              onOpen && r.judge?.overall_score != null
                ? "link inline-flex cursor-pointer"
                : "inline-flex cursor-help"
            }
            onClick={
              onOpen && r.judge?.overall_score != null
                ? () => onOpen(sid, "detail")
                : undefined
            }
          >
            <CaseBadge score={score} />
          </span>
        </Td>
        <Td className="text-xs text-muted-foreground">
          <div title="业务发生时间（trace 时间）">{fmtTime(r.timestamp)}</div>
          <div className="mt-1 text-[11px] opacity-70" title="拉取入库时间">
            拉取 {fmtTime(r.ingested_at)}
          </div>
        </Td>
      </tr>
    );
  });
  const table = (
    <table className="w-full border-collapse">
      <thead>
        <tr>
          {selectable && (
            <Th>
              <input
                type="checkbox"
                checked={allChecked}
                onChange={(e) => onSelectAll?.(e.target.checked)}
                title="全选本页"
              />
            </Th>
          )}
          <Th>会话</Th>
          <Th>提交人</Th>
          <Th>轮数</Th>
          <Th>Trace ID</Th>
          <Th>Used Skills</Th>
          <Th>状态</Th>
          <Th>价值判定</Th>
          <Th>评审结论</Th>
          <Th>时间 / 拉取</Th>
        </tr>
      </thead>
      <tbody>{body}</tbody>
    </table>
  );
  return (
    // Keeps the table at its natural column widths and scrolls horizontally
    // instead of squeezing/clipping columns on narrow viewports.
    <div className="overflow-auto [scrollbar-width:thin]" style={maxHeight ? { maxHeight } : undefined}>
      {table}
    </div>
  );
}
