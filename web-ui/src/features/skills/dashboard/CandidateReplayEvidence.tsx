import { Pill } from "@/components/common";
import type { EvalResult, ReplayDatasetResult, ReplaySide } from "@/features/replay/api/client";

const METRICS = [
  ["interaction_turns", "交互轮次"], ["tool_call_count", "工具调用"], ["total_tokens", "Tokens"],
] as const;

export function formatMetric(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "不可用";
}

export function EfficiencyTable({ efficiency }: {
  efficiency?: NonNullable<EvalResult["replay"]>["efficiency"];
}) {
  return (
    <table className="w-full border-collapse text-xs" aria-label="A/B 效率对比">
      <thead className="bg-surface-subtle text-muted-foreground">
        <tr>{["指标", "基线", "候选", "变化"].map(label => <th key={label} className="px-3 py-2 text-left">{label}</th>)}</tr>
      </thead>
      <tbody>
        {METRICS.map(([key, label]) => {
          const metric = efficiency?.dimensions?.[key];
          const delta = metric?.delta;
          return (
            <tr key={key} className="border-t border-border">
              <td className="px-3 py-2 font-semibold">{label}</td>
              <td className="px-3 py-2">{formatMetric(metric?.baseline)}</td>
              <td className="px-3 py-2">{formatMetric(metric?.candidate)}</td>
              <td className={`px-3 py-2 ${typeof delta === "number" && delta > 0 ? "text-success" : ""}`}>
                {typeof delta !== "number" ? "不可用" : delta > 0 ? `减少 ${formatMetric(delta)}`
                  : delta < 0 ? `增加 ${formatMetric(-delta)}` : "持平"}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function BranchEvidence({ title, branch }: { title: string; branch?: ReplaySide }) {
  if (!branch) return <div className="p-3 text-xs text-muted-foreground">{title}：结果不可用</div>;
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface-subtle p-3">
      <div className="mb-2 text-xs font-bold">{title}</div>
      <div className="mb-3 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
        {METRICS.map(([key, label]) => <span key={key}>{label} {formatMetric(branch[key])}</span>)}
      </div>
      {branch.error && <div className="mb-2 text-xs text-destructive">{branch.error}</div>}
      <details open>
        <summary className="cursor-pointer text-xs font-semibold">完整回放对话</summary>
        <div className="mt-2 space-y-2">
          {branch.interactions?.length ? branch.interactions.map((turn, index) => (
            <div key={index} className="rounded border border-border bg-background p-2 text-xs">
              <div className="mb-1 text-[11px] font-semibold text-muted-foreground">第 {index + 1} 轮 · 用户</div>
              <pre className="whitespace-pre-wrap break-words font-sans">{String(turn.prompt || "")}</pre>
              <div className="mb-1 mt-2 text-[11px] font-semibold text-muted-foreground">Agent</div>
              <pre className="whitespace-pre-wrap break-words font-sans">{String(turn.response || "")}</pre>
            </div>
          )) : <pre className="whitespace-pre-wrap break-words text-xs font-sans">
            {branch.final_response || branch.response || branch.response_text || "（无输出）"}
          </pre>}
        </div>
      </details>
      <details open className="mt-3">
        <summary className="cursor-pointer text-xs font-semibold">
          Checklist · {branch.checklist_report?.satisfied_count ?? 0}/{branch.checklist_report?.total ?? 0}
        </summary>
        <div className="mt-2 space-y-2">
          {(branch.checklist_report?.items || []).map((item, index) => (
            <div key={item.id || index} className="rounded border border-border p-2 text-[11px]">
              <div className="flex items-start gap-2">
                <Pill tone={item.satisfied ? "green" : "amber"}>{item.satisfied ? "满足" : "未满足"}</Pill>
                <span>{item.id} {item.text}</span>
              </div>
              {item.evidence && <div className="mt-1 text-muted-foreground">{item.evidence}</div>}
            </div>
          ))}
        </div>
      </details>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs font-semibold">工具轨迹与产物</summary>
        <pre className="mt-2 max-h-[400px] overflow-auto whitespace-pre-wrap break-words text-[11px]">
          {branch.trajectory || JSON.stringify(branch.messages || [], null, 2)}
        </pre>
        {!!branch.artifacts?.length && <pre className="mt-2 max-h-[400px] overflow-auto whitespace-pre-wrap break-words text-[11px]">
          {JSON.stringify(branch.artifacts, null, 2)}
        </pre>}
      </details>
    </div>
  );
}

export default function CandidateReplayEvidence({ datasets, sampling }: {
  datasets: ReplayDatasetResult[];
  sampling?: NonNullable<EvalResult["replay"]>["sampling"];
}) {
  return (
    <div className="min-w-0 space-y-3 overflow-x-hidden" aria-label="所有数据集回放内容">
      <div className="rounded-lg border border-border bg-surface-subtle p-3 text-xs">
        <div className="flex flex-wrap items-center gap-2 font-semibold">
          <span>回放覆盖 {datasets.length} 个数据集 · {datasets.reduce((sum, dataset) => sum + dataset.total, 0)} 个 Case</span>
        </div>
        <div className="mt-1 text-muted-foreground">
          当次数据集全部回放 · 历史采样 {sampling?.historical_selected ?? datasets.filter(d => d.selection === "historical").reduce((n, d) => n + d.total, 0)}/{sampling?.historical_requested ?? 3} 条
        </div>
      </div>
      {datasets.map(dataset => (
        <details open key={dataset.dataset_id} className="min-w-0 overflow-hidden rounded-lg border border-border">
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 bg-surface-subtle px-4 py-3 text-xs">
            <Pill tone={dataset.selection === "historical" ? "gray" : "blue"}>
              {dataset.selection === "historical" ? "历史采样" : "当次数据集"}
            </Pill>
            <span className="font-bold">{dataset.name}</span>
            <span className="text-muted-foreground">v{dataset.revision ?? 1} · {dataset.completed}/{dataset.total} Case</span>
            <Pill tone={dataset.result.verdict === "accept" ? "green" : dataset.result.verdict === "reject" ? "red" : "amber"}>
              {dataset.result.verdict === "accept" ? "通过" : dataset.result.verdict === "reject" ? "未通过" : "无法确定"}
            </Pill>
          </summary>
          <div className="border-t border-border"><EfficiencyTable efficiency={dataset.result.efficiency} /></div>
          <div className="space-y-3 p-3">
            {dataset.inputs.map((input, index) => {
              const pair = dataset.result.cases?.find(caseResult => caseResult.case_id === input.case_id);
              return (
                <div key={input.case_id} className="min-w-0 rounded-lg border border-border p-3">
                  <div className="mb-2 text-[11px] font-semibold text-muted-foreground">
                    Case {index + 1} · {input.metadata?.name || input.case_id}
                  </div>
                  <pre className="mb-3 whitespace-pre-wrap break-words text-xs font-sans">{input.query}</pre>
                  {!!input.materials?.length && <div className="mb-2 text-xs text-muted-foreground">
                    材料：{input.materials.map(material => material.path).join("、")}
                  </div>}
                  {!pair && <div className="mb-2 text-xs text-destructive">该 Case 没有完整回放结果。{dataset.result.reason}</div>}
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <BranchEvidence title="A · 基线" branch={pair?.baseline} />
                    <BranchEvidence title="B · 候选" branch={pair?.candidate} />
                  </div>
                </div>
              );
            })}
          </div>
        </details>
      ))}
    </div>
  );
}
