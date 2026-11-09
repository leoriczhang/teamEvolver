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
    allowed_modules: result?.allowed_modules ?? row.allowed_modules ?? ["skill"],
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
  if (cycle.reason === "no_verifiable_new_evidence" || cycle.zero_candidate_reason === "no_verifiable_new_evidence") {
    return { label: "已完成 · 无有效新证据", tone: "gray" };
  }
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
    no_verifiable_new_evidence: "没有可核验的新增来源证据；本次未生成候选，不代表业务效果提升。",
  };
  return reasons[reason] || reason;
}

export function auditAction(action?: string): string {
  return ({ skip: "跳过", candidate_created: "已生成候选", create: "新建", update: "更新", merge: "合并" })[
    action as "skip" | "candidate_created" | "create" | "update" | "merge"
  ] || action || "未记录动作";
}

export function auditGroupName(group: NonNullable<EvolveHistoryCycle["evolutions"]>[number]): string {
  const key = group.group_key || group.skill_name || "";
  if (group.candidate_kind === "execution" || key.startsWith("execution:")) {
    return `业务执行配置：${group.execution_profile_id || key.slice(10) || "未记录"}`;
  }
  return group.skill_name === "__no_skill__" ? "未关联 Skill 的会话组" : group.skill_name || "-";
}
