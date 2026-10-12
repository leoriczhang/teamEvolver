import type { EvalResult, ReplaySide } from "@/api/client";

export type ReplayEvaluationState = "pending" | "completed" | "skipped" | "not_run" | "failed";

/** Unknown counters must stay distinct from an observed zero. */
export function observedMetric(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function formatReplayMetric(value: unknown): string {
  const number = observedMetric(value);
  return number === null ? "未观测" : number.toLocaleString();
}

export function replayIssue(ev?: EvalResult | null): string {
  if (!ev) return "";
  if (ev.replay?.error) return ev.replay.error;
  for (const item of ev.replay?.cases || []) {
    for (const branch of [item.baseline, item.candidate]) {
      if (branchFailed(branch)) return branch?.error || branch?.reason || branch?.error_code || branch?.status || "回放分支失败";
    }
  }
  return ev.replay?.reason || "";
}

function hasReplayError(ev: EvalResult): boolean {
  return !!ev.replay?.error || !!ev.replay?.cases?.some((item) => branchFailed(item.baseline) || branchFailed(item.candidate));
}

function branchFailed(branch?: ReplaySide): boolean {
  return !!branch?.error || branch?.ok === false || !!branch?.error_code ||
    /^(failed|error|unsupported|skipped|not_run)$/.test(branch?.status || "");
}

function branchObserved(branch?: ReplaySide): boolean {
  return !!branch && (branch.ok === true || /^(completed|ok)$/.test(branch.status || "") ||
    branch.response !== undefined || branch.final_response !== undefined || branch.response_text !== undefined ||
    [branch.interaction_turns, branch.tool_call_count, branch.total_tokens].some((metric) => observedMetric(metric) !== null));
}

export function replayEvaluationState(ev?: EvalResult | null): ReplayEvaluationState {
  if (!ev) return "pending";
  const replay = ev.replay;
  const status = replay?.status || ev.status;
  if (status === "skipped" || status === "not_run" || status === "failed") return status;
  if (hasReplayError(ev)) return "failed";
  const cases = replay?.cases || [];
  if (status === "completed") {
    return (observedMetric(replay?.case_count) ?? cases.length) > 0 ? "completed" : "not_run";
  }
  // Older records used "evaluated" even when every case was unsupported.
  // Only paired branch observations establish completion for those records.
  if ((!status || status === "evaluated") && cases.length > 0 &&
      !/ReplayUnsupported|unsupported|skipped/i.test(replay?.reason || "") &&
      cases.every((item) => branchObserved(item.baseline) && branchObserved(item.candidate))) {
    return "completed";
  }
  return "not_run";
}

export function isReplayCompleted(ev?: EvalResult | null): boolean {
  return replayEvaluationState(ev) === "completed";
}

export function canPublishFromReplay(ev?: EvalResult | null): boolean {
  return isReplayCompleted(ev) && ev?.recommended_publish === true &&
    ev.replay?.verdict !== "reject" && ev.replay?.verdict !== "inconclusive" &&
    ev.replay?.decision_policy?.accepted !== false &&
    ev.replay?.decision_policy?.verdict !== "reject" && ev.replay?.decision_policy?.verdict !== "inconclusive";
}

export function replayStateLabel(ev?: EvalResult | null): string {
  return ({ pending: "待评估", completed: "已完成", skipped: "已跳过", not_run: "未执行", failed: "回放失败" })[replayEvaluationState(ev)];
}

export function replayConclusion(ev?: EvalResult | null): string {
  if (!ev) return "—";
  if (!isReplayCompleted(ev)) return "未完成回放";
  const basis = ev.replay?.decision_policy?.decision_basis;
  if (basis === "candidate_checklist_incomplete" || basis === "both_checklists_incomplete") return "完成条件未通过";
  if (basis === "checklist_judge_unavailable") return "完成条件未判定";
  if (canPublishFromReplay(ev)) return "建议发布";
  const dimensions = ev.replay?.efficiency?.dimensions;
  const observed = ["interaction_turns", "tool_call_count", "total_tokens"].every((key) => {
    const metric = dimensions?.[key];
    return metric && observedMetric(metric.baseline) !== null && observedMetric(metric.candidate) !== null &&
      observedMetric(metric.delta) !== null;
  });
  return observed ? "持平 / 有增加" : "指标未观测";
}
