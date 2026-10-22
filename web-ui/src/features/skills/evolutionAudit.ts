import { type EvolveHistoryCycle } from "@/features/skills/api/client";
import { type PillTone } from "@/components/common";

export interface AuditCycle extends EvolveHistoryCycle {
  hasResult: boolean;
  taskStatus: string;
  taskError?: string;
}

export interface AuditJob {
  status: string;
  error_code?: string;
}

/** Read the persisted command envelope while retaining older flat audit records. */
export function auditCycle(row: EvolveHistoryCycle): AuditCycle | null {
  const nested = Object.prototype.hasOwnProperty.call(row, "result");
  if (nested && !row.command_id?.startsWith("evolve:")) return null;
  const result = nested ? row.result : row;
  const evolutions = result?.evolutions || [];
  const sessionIds = result?.session_ids ?? [...new Set(evolutions.flatMap(e => e.session_ids || []))];
  return {
    ...row,
    ...result,
    timestamp: row.created_at || row.timestamp,
    session_ids: sessionIds,
    skill_groups: result?.skill_groups ?? evolutions.length,
    candidates_queued: result?.candidates_queued ?? evolutions.filter(e => e.action === "candidate_created").length,
    hasResult: Boolean(result),
    taskStatus: result ? "succeeded" : "unknown",
  };
}

export function needsAuditRefresh(cycle: AuditCycle): boolean {
  return !cycle.hasResult && !["failed", "cancelled"].includes(cycle.taskStatus);
}

export function auditState(cycle: AuditCycle): { label: string; tone: PillTone } {
  if (!cycle.hasResult) {
    const states: Record<string, { label: string; tone: PillTone }> = {
      queued: { label: "排队中", tone: "gray" },
      running: { label: "运行中", tone: "blue" },
      retry_wait: { label: "等待重试", tone: "amber" },
      waiting_external: { label: "等待外部任务", tone: "amber" },
      reconciling: { label: "核对中", tone: "amber" },
      failed: { label: "失败", tone: "red" },
      cancelled: { label: "已取消", tone: "gray" },
      succeeded: { label: "任务完成，结果待同步", tone: "amber" },
    };
    return states[cycle.taskStatus] || { label: "状态待确认", tone: "gray" };
  }
  if (cycle.had_processing_error || cycle.status === "failed") return { label: "处理异常", tone: "red" };
  if (Number(cycle.uploaded_skills) > 0) return { label: "已产出", tone: "green" };
  if (Number(cycle.candidates_queued) > 0) return { label: "已完成 · 已生成候选", tone: "amber" };
  if (cycle.evolutions?.length && cycle.evolutions.every(e => e.action === "skip")) {
    return { label: "已完成 · 全部分组跳过", tone: "gray" };
  }
  return { label: "已完成 · 无候选", tone: "blue" };
}

export function auditReason(reason: string): string {
  const reasons: Record<string, string> = {
    "insufficient distinct Sessions for team evidence": "不同 Session 数不足，未满足团队 Evidence 门槛。",
    "insufficient distinct users for team evidence": "不同用户数不足，未满足团队 Evidence 门槛。",
  };
  return reasons[reason] || reason;
}

export function auditAction(action?: string): string {
  return ({ skip: "跳过", candidate_created: "已生成候选", create: "新建", update: "更新", merge: "合并" })[
    action as "skip" | "candidate_created" | "create" | "update" | "merge"
  ] || action || "未记录动作";
}
