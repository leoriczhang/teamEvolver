import type { ExecutionProfile } from "@/features/skills/api/client";
import { EfficiencyTable } from "@/features/skills/dashboard/CandidateReplayEvidence";
import ExternalEvaluationResult from "@/features/replay/ExternalEvaluationResult";
import { fmtTime } from "@/lib/format";
import type { ExecutionReleaseDetail } from "./types";

function caseSummary(report?: {
  case_count?: number | null;
  completed_count?: number | null;
  reports?: Array<{ all_satisfied?: boolean }>;
}) {
  if (!report) return "未记录";
  if (report.case_count != null) return `${report.completed_count ?? "未知"}/${report.case_count} 个 Case 全部满足`;
  if (report.reports?.length && report.reports.every(item => typeof item.all_satisfied === "boolean")) {
    return `${report.reports.filter(item => item.all_satisfied).length}/${report.reports.length} 个 Case 全部满足`;
  }
  return "Case 通过数未记录";
}

function itemSummary(report?: { satisfied_count?: number; total?: number }) {
  return `${report?.satisfied_count ?? "未知"}/${report?.total ?? "未知"} 项满足`;
}

const verdictLabels: Record<string, string> = { accept: "通过", reject: "未通过", inconclusive: "无法确定" };
const gateLabels: Record<string, string> = { passed: "通过", failed: "未通过", inconclusive: "无法确定", not_required: "无需检查" };

export function ExecutionVersionMetrics({ detail, onSelectRelease }: {
  detail: ExecutionReleaseDetail;
  onSelectRelease: (releaseId: string) => void;
}) {
  const validation = detail.validation;
  const evaluation = validation.evaluation;
  const verdict = evaluation?.validation_verdict || evaluation?.replay?.verdict;
  const gate = evaluation?.execution_gate;
  return <section aria-label="所选发布 Replay 指标" className="space-y-4 text-xs">
    <p className="rounded-md border border-border bg-surface-subtle p-3 text-muted-foreground">
      指标只对应这个已发布版本当时冻结的验证结果；切换历史版本不会启动评估或调用模型。
    </p>
    {validation.status === "available" && evaluation ? <>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg border border-border p-3"><p className="text-muted-foreground">Replay 质量判断</p><p className="mt-2 text-base font-semibold">{verdictLabels[verdict || ""] || "未记录"}</p></div>
        <div className="rounded-lg border border-border p-3"><p className="text-muted-foreground">旧版 Checklist</p><p className="mt-2 font-semibold">{caseSummary(evaluation.replay?.checklist?.baseline)}</p><p className="mt-1 text-muted-foreground">{itemSummary(evaluation.replay?.checklist?.baseline)}</p></div>
        <div className="rounded-lg border border-border p-3"><p className="text-muted-foreground">新版 Checklist</p><p className="mt-2 font-semibold">{caseSummary(evaluation.replay?.checklist?.candidate)}</p><p className="mt-1 text-muted-foreground">{itemSummary(evaluation.replay?.checklist?.candidate)}</p></div>
      </div>
      <ExternalEvaluationResult report={evaluation.replay?.external_evaluation} policy={evaluation.replay?.decision_policy} />
      <div className="overflow-x-auto rounded-lg border border-border"><EfficiencyTable efficiency={evaluation.replay?.efficiency} /></div>
      <p className="text-muted-foreground">上表比较当次冻结基线和所选发布候选的回放结果，数值不可用时不会按 0 计算。</p>
      <div className="space-y-1 break-all rounded-lg bg-muted p-3">
        <div>候选：{validation.candidate_id || detail.release.candidate_id || "未记录"} · 修订 {validation.candidate_revision ?? detail.release.candidate_revision ?? "未记录"}</div>
        <div>验证运行：{validation.run_id || "未记录"}</div>
        <div>实际加载核验：{gateLabels[gate?.status || ""] || gate?.status || "未记录"}</div>
        {gate?.reason && <div>{gate.reason}</div>}
        <div>评估指纹：{validation.evaluation_hash || "未记录"}</div>
        {evaluation.replay?.evaluation_policy_hash && <div>评价配置指纹：{evaluation.replay.evaluation_policy_hash}</div>}
      </div>
    </> : <div className="space-y-2 rounded-lg border border-dashed border-border p-4 text-muted-foreground">
      <p>{validation.status === "not_applicable"
        ? validation.source_release_id ? "此版本是组合回滚记录，不代表重新进行了一轮 Replay。" : "此版本是初始登记基线，没有新旧版本对比指标。"
        : "此历史发布缺少可核对的冻结验证记录，暂不展示 Replay 指标。"}</p>
      {validation.source_release_id && <button type="button" className="break-all text-primary underline underline-offset-2"
        onClick={() => onSelectRelease(validation.source_release_id!)}>查看回滚来源版本 · {validation.source_release_id}</button>}
      {validation.reason && <p className="break-all text-[11px]">记录说明：{validation.reason}</p>}
    </div>}
  </section>;
}

export function ExecutionVersionReceipts({ detail, profile, receiptsStatus }: {
  detail: ExecutionReleaseDetail;
  profile: ExecutionProfile | null;
  receiptsStatus: "loading" | "failed" | "ready";
}) {
  const release = detail.release;
  const receipts = (profile?.receipts || []).filter(receipt => receipt.release_id === release.release_id);
  return <section aria-label="所选发布与 Agent 回执" className="space-y-4 text-xs">
    <div className="space-y-2 break-all rounded-lg border border-border p-4">
      <p className="font-semibold">TE 发布记录{release.release_id === detail.current_release_id ? " · 当前发布" : " · 历史发布"}</p>
      <p>发布 ID：{release.release_id}</p><p>发布时间：{fmtTime(release.created_at)}</p>
      <p>组合哈希：{release.composition_hash}</p>
      {release.restored_from && <p>回滚来源：{release.restored_from}</p>}
      <p>Skill 依赖：{release.document.skill_dependencies?.length
        ? release.document.skill_dependencies.map(skill => `${skill.name} v${skill.version}`).join("、") : "无（纯 Prompt 组合）"}</p>
    </div>
    <div className="space-y-3">
      <h3 className="font-semibold">Agent 下载／激活回执</h3>
      <p className="text-muted-foreground">以下是 Agent 自报的历史事件，不代表当前仍在使用；模型是否实际加载需结合请求证据核对。已发布也不代表已采用。</p>
      {receiptsStatus === "loading" && <p role="status" className="text-muted-foreground">正在读取 Agent 回执…</p>}
      {receiptsStatus === "failed" && <p className="text-destructive">回执加载失败，当前无法判断是否存在下载或激活记录。</p>}
      {receiptsStatus === "ready" && !receipts.length && <p className="rounded-md bg-muted p-3 text-muted-foreground">最近的回执中，没有这个版本的下载或激活记录。</p>}
      {receiptsStatus === "ready" && receipts.map((receipt, index) => <div key={receipt.request_id || index} className="space-y-1 break-all rounded-md bg-muted p-3">
        <div className="font-medium">{receipt.state === "activated" ? "Agent 自报已激活" : "Agent 自报已下载"} · {fmtTime(receipt.at)}</div>
        <div className="text-muted-foreground">证据级别：agent_reported · 组合哈希：{receipt.composition_hash}</div>
      </div>)}
      <p className="text-[11px] text-muted-foreground">回执来自此执行配置最近的记录；更早的事件可能未包含在本次列表中。</p>
    </div>
  </section>;
}
