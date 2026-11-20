import { api } from "@/api/client";
import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { BranchEvidence,EfficiencyTable } from "@/features/skills/dashboard/CandidateReplayEvidence";
import type { EvalResult,ReplayCase } from "./api/client";
import { useEffect,useState } from "react";

type Run = {
  status: string;
  subject_id: string;
  total: number;
  completed: number;
  error_code?: string;
  result?: {
    verdict?: string;
    reason?: string;
    cases?: ReplayCase[];
    efficiency?: NonNullable<EvalResult["replay"]>["efficiency"];
  };
};

function reasonLabel(reason: string) {
  const labels: Record<string, string> = {
    secondary_metrics_increased: "交互轮次持平，候选的工具调用或 Token 消耗增加。",
    branch_failure: "至少一条分支执行失败，请查看分支错误和工具轨迹。",
    metrics_unavailable: "缺少真实用量指标，暂不能比较效果。",
    interaction_turns_decreased: "候选以更少的交互轮次完成了任务。",
    baseline_checklist_incomplete: "基线分支尚未满足全部完成条件。",
    candidate_checklist_incomplete: "候选分支尚未满足全部完成条件。",
  };
  return labels[reason] || reason;
}

export default function ReplayRunView({ runId, active, onBack }: {
  runId: string; active: boolean; onBack: () => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const value = await api<Run>(`/api/replays/skill/${encodeURIComponent(runId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setRun(value); setError("");
        if (["queued", "running", "retry_wait"].includes(value.status)) timer = setTimeout(load, 3000);
      } catch (e: any) {
        if (!controller.signal.aborted) setError(e.message);
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, runId, tick]);
  return <div className="space-y-4 px-[22px] pb-8 pt-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" onClick={onBack}>返回数据集</Button>
      <Button variant="outline" onClick={() => setTick(t => t + 1)}>刷新回放</Button>
    </div>
    <section className="rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">Skill A/B 回放</h2>
      <p className="mt-1 break-all text-xs text-muted-foreground">{runId}</p>
      {run && <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <Pill tone={run.status === "succeeded" ? "green" : run.status === "failed" ? "red" : "blue"}>
          {run.status === "succeeded" ? "执行完成" : run.status === "running" ? "运行中" : run.status}
        </Pill>
        <span>{run.subject_id} · {run.completed}/{run.total} Case</span>
        {run.result?.verdict && <span>效果结论：{run.result.verdict === "accept" ? "通过"
          : run.result.verdict === "reject" ? "未通过" : "无法确定"}</span>}
      </div>}
      {run?.result?.reason && <p className="mt-2 text-xs text-muted-foreground">{reasonLabel(run.result.reason)}</p>}
      {run?.error_code && <p className="mt-2 text-xs text-destructive">{run.error_code}</p>}
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
      {!run && !error && <p className="mt-3 text-sm text-muted-foreground">正在读取回放记录…</p>}
    </section>
    {run?.result?.cases?.map((pair, index) => <section key={pair.case_id || index}
      className="space-y-3 rounded-xl border border-border bg-surface p-3">
      <h3 className="text-sm font-semibold">Case {index + 1}</h3>
      <EfficiencyTable efficiency={run.result?.efficiency} />
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <BranchEvidence title="A · 基线" branch={pair.baseline} />
        <BranchEvidence title="B · 候选" branch={pair.candidate} />
      </div>
    </section>)}
  </div>;
}
