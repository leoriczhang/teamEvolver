import type { ExecutionCostGate, ExecutionProbeGate, ExecutionValidationPlan } from "@/features/skills/api/client";
import { isStandardValidation } from "@/features/skills/api/client";

const statuses: Record<string, string> = {
  ready: "已锁定", passed: "通过", failed: "失败", inconclusive: "证据不足／阻断",
  not_required: "无需执行",
};
const reasons: Record<string, string> = {
  holdout_not_configured: "尚未配置锁定验收数据集",
  probe_coverage_exceeds_budget: "探测案例预算无法覆盖全部受影响 Skill",
  probe_runs_exceed_budget: "所需探测运行次数超过预算",
  total_cost_unknown: "主验证的 Token 成本未知",
  probe_cost_unknown: "探测运行的 Token 成本未知",
  probe_cost_budget_exhausted: "探测运行的 Token 预算已耗尽",
  total_cost_exceeded_budget: "本次 Replay 的 Token 总量超过预算",
  probe_plan_incomplete: "探测计划尚未完整执行",
  repeat_verdict_unstable: "重复运行的裁决不稳定",
  repeat_completion_unstable: "重复运行的完成结果不稳定",
  aa_assets_differ: "A/A 对照使用的资产不一致",
  aa_completion_unstable: "A/A 对照的完成结果不稳定",
  total_model_cost_unknown: "累计模型成本不完整，不能确认满足预算",
  worker_retry_cost_unreconciled: "Worker 重试成本尚未核对完整",
  total_model_cost_exceeds_budget: "候选累计模型 Token 已超过预算",
  replay_te_llm_accounting_unverified: "历史裁判／反馈成本未完整采集，不能确认总成本满足预算",
};
const accountingWarnings: Record<string, string> = {
  total_model_cost_unknown: "累计用量记录不完整，已知 Token 仅为下界，总用量未知",
  worker_retry_cost_unreconciled: "历史 Worker 重试用量尚未核对完整",
  replay_te_llm_accounting_unverified: "历史裁判／反馈用量未完整采集",
};

function runName(kind: string) {
  if (kind === "aa") return "A/A 同资产对照";
  if (kind.startsWith("repeat-")) return `重复运行 ${kind.slice(7)}`;
  return ({
    "only-skill": "单项消融：仅 Skill",
    "only-agent_prompt": "单项消融：仅业务 Prompt",
    "only-context_fragment": "单项消融：仅上下文注入",
  } as Record<string, string>)[kind] || kind;
}

function budgetLabel(value: number | null | undefined) {
  return value === null ? "无上限" : value ?? "未记录";
}

export default function CandidateValidationPanel({ plan, probe, cost }: {
  plan?: ExecutionValidationPlan;
  probe?: ExecutionProbeGate;
  cost?: ExecutionCostGate;
}) {
  if (!plan && !probe && !cost) return null;
  const holdout = plan?.holdout;
  const standard = isStandardValidation(plan);
  const hasAccountingStatus = cost?.accounting_status === "complete" || cost?.accounting_status === "incomplete";
  const costAccountingComplete = cost?.accounting_status === "complete" && typeof cost?.total_tokens === "number";
  const legacyCoverageMissing = !hasAccountingStatus && cost?.replay_te_llm_accounting_complete !== true;
  const costStatus = legacyCoverageMissing ? "inconclusive" : cost?.status;
  const costReasons = [...new Set([
    ...(cost?.reasons || []),
    ...(legacyCoverageMissing ? ["replay_te_llm_accounting_unverified"] : []),
  ])];
  const costWarnings = [...new Set(cost?.warnings || [])];
  return <section aria-label={standard ? "候选验证策略" : "锁定验收与稳定性探测"} className="space-y-2 rounded-lg border border-border p-3 text-xs">
    <h4 className="font-semibold">{standard ? "Skill 默认验证策略" : "锁定验收与稳定性探测"}</h4>
    {standard && <p className="text-muted-foreground">沿用 Skill 的静态检查、运行与主 True Replay 判定，并核对业务组件实际加载和版本。锁定验收、A/A、重复运行及独立外部复核不是本次默认发布条件。</p>}
    <div>盲测／锁定验收：{standard ? "非必需" : statuses[holdout?.status || ""] || holdout?.status || "未记录"}</div>
    {!!holdout?.datasets?.length && <div className="break-all text-muted-foreground">
      锁定 Dataset：{holdout.datasets.map(dataset => `${dataset.dataset_id}（v${dataset.revision ?? "?"}）`).join("、")}
    </div>}
    {holdout?.case_ids && <div>锁定验收 Case：{holdout.case_ids.length} 个</div>}
    {(holdout?.reasons || []).map(reason => <div key={reason} className="text-destructive">验收阻断：{reasons[reason] || reason}</div>)}
    {plan?.required_skill_ids && <div>共享回归范围：{plan.required_skill_ids.join("、") || "0 Skill（纯业务组合）"}</div>}
    <div>稳定性／消融门禁：{standard ? "非必需" : statuses[probe?.status || ""] || probe?.status || "尚未执行"}</div>
    {probe && <div>本次 Agent Replay Token（含主验证与探测）：{probe.total_replay_tokens ?? "未知"} / 本次 Replay 预算 {budgetLabel(probe.token_budget)}</div>}
    {probe?.attribution && <p className="text-muted-foreground">
      {probe.attribution === "single_change_comparisons"
        ? "已运行单项消融比较；此处记录比较结果，不据此自动证明因果。"
        : "仅能归因于本次单模块或组合，尚无单项消融归因。"}
    </p>}
    {(probe?.reasons || []).map(reason => <div key={reason} className="text-destructive">探测阻断：{reasons[reason] || reason}</div>)}
    {!!probe?.runs?.length && <div className="space-y-1">
      {probe.runs.map((run, index) => <div key={run.job_id || `${run.kind}:${index}`} className="rounded-md bg-muted p-2">
        <div>{runName(run.kind)} · 裁决：{run.verdict || "未记录"} · 实际加载：{statuses[run.execution_gate?.status || ""] || run.execution_gate?.status || "未记录"}</div>
        {(run.execution_gate?.reasons || []).map(reason => <div key={reason} className="break-all text-destructive">{reason}</div>)}
      </div>)}
    </div>}
    {cost && <div className="space-y-1 rounded-md bg-muted p-2">
      <div className="font-semibold">候选累计预算检查：{statuses[costStatus || ""] || costStatus || "未记录"}{cost.budget_tokens === null && costStatus === "passed" ? "（无上限）" : ""}</div>
      <div>已知累计 Token（去重）：{cost.known_total_tokens ?? "未知"} / 候选预算 {budgetLabel(cost.budget_tokens)}</div>
      <div>累计总 Token：{costAccountingComplete ? cost.total_tokens : "未知"}</div>
      <div>累计统计：{costAccountingComplete ? "完整" : hasAccountingStatus ? "记录不完整；总用量未知" : "完整性未记录；总用量未知"}</div>
      {!costAccountingComplete && <p className="text-muted-foreground">已知 Token 仅为成本下界，未记录的用量不补零；历史记录保持原样。</p>}
      <div>累计 Agent Replay Token：{cost.agent_replay_tokens ?? "未知"}</div>
      <div>累计 TE 模型 Token：{cost.te_llm_usage?.known_total_tokens ?? "未知"} / 已记录但用量未知的调用：{cost.te_llm_usage?.unknown_attempts ?? "未记录"}</div>
      <div>纳入的此前验证：{cost.prior_validation_runs ?? "未记录"} 次</div>
      <p className="text-[11px] text-muted-foreground">累计统计按调用 ID 去重，包含候选生成、裁判、已执行探测及此前各次验证。无上限时，历史用量缺口作为统计提示；有限预算仍按授权检查。本次发布要求以冻结的验证策略为准。</p>
      {costReasons.map(reason => <div key={reason} className="text-destructive">成本阻断：{reasons[reason] || reason}</div>)}
      {costWarnings.map(warning => <div key={warning} className="text-muted-foreground">统计提示{cost.budget_tokens === null ? "（不阻断预算检查）" : ""}：{accountingWarnings[warning] || warning}</div>)}
    </div>}
    <p className="text-[11px] text-muted-foreground">这些状态记录验证覆盖、稳定性与成本，不代表候选已经带来业务收益。</p>
  </section>;
}
