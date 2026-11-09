import type { ExternalEvaluationReport, ReplayDecisionPolicy } from "./api/client";

function score(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumSignificantDigits: 8 }) : "不可用";
}

export default function ExternalEvaluationResult({ report, policy }: {
  report?: ExternalEvaluationReport;
  policy?: ReplayDecisionPolicy;
}) {
  if (!report && policy?.policy !== "external-agent-score") return null;
  const baseline = report?.baseline?.score ?? report?.baseline_score;
  const candidate = report?.candidate?.score ?? report?.candidate_score;
  const delta = report?.delta ?? report?.score_delta
    ?? (typeof baseline === "number" && typeof candidate === "number" ? candidate - baseline : undefined);
  const completeScores = typeof baseline === "number" && Number.isFinite(baseline)
    && typeof candidate === "number" && Number.isFinite(candidate);
  const unavailable = !report || report.status === "unavailable" || !completeScores
    || (report.status !== "evaluated" && !(report.case_count && report.evaluated_case_count === report.case_count));
  const reason = ({
    external_score_improved: "候选平均分提升，并达到配置的采纳门槛。",
    external_score_tied: "新旧平均分持平，无法确认改善。",
    external_score_below_requirement: "候选平均分下降，或平均分／提升幅度未达到配置要求。",
    external_evaluation_unavailable: "评分不完整，本次无法确定。",
  } as Record<string, string>)[report?.decision_basis || policy?.decision_basis || ""];
  const minScore = report?.min_score ?? policy?.min_score;
  const minImprovement = report?.min_improvement ?? policy?.min_improvement;
  return (
    <section aria-label="外部评价 Agent 评分" className="space-y-2 rounded-lg border border-border bg-surface-subtle p-3 text-xs">
      <div className="font-semibold">外部评价 Agent · {report?.case_count ? `${report.case_count} 个 Case 平均分` : "业务评分"}</div>
      {unavailable ? <p className="text-destructive">外部评分不可用：{report?.error
        || (report?.error_code === "external_evaluation_branch_failed" ? "真实回放分支未成功执行，不能进行评分。" : "未获得完整评分，本次无法确定。")}</p> : <>
        <table className="w-full text-left" aria-label="A/B 外部评分对比">
          <thead><tr><th className="py-1">基线平均分</th><th>候选平均分</th><th>候选－基线</th></tr></thead>
          <tbody><tr><td>{score(baseline)}</td><td>{score(candidate)}</td><td>{score(delta)}</td></tr></tbody>
        </table>
        {report?.baseline?.reason && <p className="whitespace-pre-wrap break-words"><span className="font-semibold">基线理由：</span>{report.baseline.reason}</p>}
        {report?.candidate?.reason && <p className="whitespace-pre-wrap break-words"><span className="font-semibold">候选理由：</span>{report.candidate.reason}</p>}
      </>}
      {reason && <p>{reason}</p>}
      {(minScore !== undefined || minImprovement !== undefined) && <p className="text-muted-foreground">
        本次冻结门槛：候选平均分至少 {score(minScore)}；提升至少 {score(minImprovement)} 分，且须为正向提升。
      </p>}
      <p className="text-muted-foreground">分数越高越好。此模式按外部评分判断；Token、轮次、工具调用和 Checklist 保留为执行记录。</p>
    </section>
  );
}
