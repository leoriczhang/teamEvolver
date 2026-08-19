import type { ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Pill, Empty, ListViewport, PaginationControls, usePagedItems } from "@/components/common";
import UnifiedDiffView from "@/components/UnifiedDiffView";
import { cn } from "@/lib/utils";
import type {
  Candidate,
  EvalResult,
  ReplaySide,
} from "@/api/client";

const EFFICIENCY_LABELS: Record<string, string> = {
  interaction_turns: "交互轮次",
  tool_call_count: "工具调用",
  total_tokens: "Tokens",
};

function SecTitle({ children }: { children: ReactNode }) {
  return <div className="mt-5 mb-2.5 flex items-center gap-2 text-[13px] font-bold">{children}</div>;
}

function candidateName(cand: Candidate | null, ev: EvalResult | null, jobId: string | null): string {
  return cand?.skill_name || cand?.candidate_skill_name || cand?.candidate_skill?.name || ev?.skill_name || jobId || "-";
}

function skillToMd(skill?: Candidate["candidate_skill"] | Candidate["current_skill"]) {
  if (!skill) return "";
  const description = String(skill.description || "").replace(/"/g, '\\"');
  return `---\nname: ${skill.name || "unknown"}\ndescription: "${description}"\ncategory: ${skill.category || "general"}\n---\n\n${skill.content || ""}\n`;
}

function replayOutput(branch?: ReplaySide): string {
  if (!branch) return "";
  return (
    branch.response ||
    branch.final_response ||
    branch.response_text ||
    branch.rationale ||
    branch.error ||
    ""
  );
}

export default function CandidateModal({
  jobId,
  cand,
  ev,
  evaluating,
  open,
  readOnly = false,
  onClose,
  onEvaluate,
}: {
  jobId: string | null;
  cand: Candidate | null;
  ev: EvalResult | null;
  evaluating: boolean;
  open: boolean;
  readOnly?: boolean;
  onClose: () => void;
  onEvaluate: (force: boolean) => void;
}) {
  const rep = ev?.replay || {};
  const replayCases = rep.cases || [];
  const efficiencyDimensions = rep.efficiency?.dimensions || {};
  const decisionPolicy = rep.decision_policy || {};
  const replayPager = usePagedItems(replayCases);
  const currentMd = ev?.current_skill_md || cand?.current_skill_md || skillToMd(cand?.current_skill || ev?.current_skill);
  const candidateMd = ev?.candidate_skill_md || cand?.candidate_skill_md || skillToMd(cand?.candidate_skill || ev?.candidate_skill) || cand?.content_preview || "";
  const skillDiff = ev?.skill_diff || cand?.skill_diff || "";
  const bundleDiff = ev?.bundle_diff || cand?.bundle_diff;
  const changedFiles = (bundleDiff?.files || []).filter((file) => file.status !== "unchanged");
  const staticValidation =
    ev?.static_validation ||
    cand?.static_validation ||
    cand?.candidate_skill?.static_validation;
  const action = ev?.proposed_action || cand?.proposed_action || "";
  const missingCurrentText = action === "create_skill"
    ? "（新建技能，无当前版本）"
    : "（候选 Job 未携带当前版本，且当前技能库未找到对应 SKILL.md）";

  let bodyInner: ReactNode;
  if (!ev) {
    bodyInner = evaluating ? (
      <Empty>正在运行 A/B 回放评估，请稍候…（首次评估需调用模型，可能耗时较久）</Empty>
    ) : readOnly ? (
      <Empty>该历史候选没有可展示的缓存评估结果。</Empty>
    ) : (
      <Empty>
        尚未评估。{" "}
        <Button variant="outline" size="sm" onClick={() => onEvaluate(false)}>
          开始评估
        </Button>
      </Empty>
    );
  } else {
    // Replay detail
    let replayHtml: ReactNode;
    if (rep.error) {
      replayHtml = <div className="text-xs text-destructive">回放失败：{rep.error}</div>;
    } else {
      if (!replayCases.length) {
        replayHtml = (
          <div className="text-xs text-muted-foreground">
            无可回放的案例（该候选未采样到可复现的会话轮次）。
          </div>
        );
      } else {
        replayHtml = (
          <>
            <ListViewport maxHeight="560px">
              {replayPager.items.map((cc, i) => {
          const b = cc.baseline || {};
          const a = cc.candidate || {};
          const instr = a.instruction || b.instruction || "";
          const turnNum = b.turn_num != null ? b.turn_num : a.turn_num;
          return (
            <div key={replayPager.start + i} className="mb-3 rounded-lg border border-border p-3.5">
              <div className="mb-2.5 text-xs text-muted-foreground">
                案例 {replayPager.start + i + 1} / {replayCases.length} &nbsp;·&nbsp; 会话{" "}
                {b.session_id || a.session_id || "?"} · 第 {turnNum ?? "?"} 轮
              </div>
              <div className="mb-2 text-xs">
                <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
                  👤 复现指令
                </div>
                {instr || <span className="text-muted-foreground">（空）</span>}
              </div>
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                <AbCol
                  head="🅰 基线（当前技能 / 无技能）"
                  branch={b}
                  body={replayOutput(b)}
                />
                <AbCol
                  head="🅱 候选（新技能）"
                  branch={a}
                  body={replayOutput(a)}
                />
              </div>
            </div>
          );
              })}
            </ListViewport>
            <PaginationControls {...replayPager} onPageChange={replayPager.setPage} />
          </>
        );
      }
    }

    bodyInner = (
      <div className="space-y-2 text-sm">
        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
            技能 / 动作
          </div>
          <div className="flex items-center gap-2">
            {candidateName(cand, ev, jobId)}
            <span className="text-muted-foreground">·</span>
            <Pill tone="blue">{action || "-"}</Pill>
          </div>
        </div>
        {cand?.rationale && (
          <div>
            <div className="mb-1.5 text-xs font-semibold text-muted-foreground">进化理由</div>
            <div>{cand.rationale}</div>
          </div>
        )}
        {cand?.evidence_classification && (
          <div>
            <div className="mb-1.5 text-xs font-semibold text-muted-foreground">证据归属</div>
            <div className="flex flex-wrap gap-1.5">
              <Pill tone="green">
                团队 SOP {cand.evidence_classification.team_skill?.length || 0}
              </Pill>
              <Pill tone="blue">
                用户 Memory 候选 {cand.evidence_classification.user_memory?.length || 0}
              </Pill>
              <Pill tone="gray">
                当前任务 {cand.evidence_classification.task_requirement?.length || 0}
              </Pill>
              <Pill tone="gray">
                运行时问题 {cand.evidence_classification.agent_runtime?.length || 0}
              </Pill>
            </div>
            {(cand.evidence_classification.user_memory?.length || 0) > 0 && (
              <div className="mt-1.5 text-xs text-muted-foreground">
                Memory 项仅为候选，不会随团队 Skill 发布。
              </div>
            )}
          </div>
        )}

        <div className="my-4 rounded-lg border border-border bg-surface-subtle p-3 text-xs">
          <div className="mb-1 font-semibold">True Replay 客观结论</div>
          <Pill
            tone={
              decisionPolicy.verdict === "accept"
                ? "green"
                : decisionPolicy.verdict === "reject"
                ? "red"
                : "amber"
            }
          >
            {decisionPolicy.decision_basis === "interaction_turns_decreased"
              ? "轮次下降，直接判定正向"
              : decisionPolicy.decision_basis === "interaction_turns_increased"
              ? "轮次上升，判定负向"
              : decisionPolicy.decision_basis === "secondary_metrics_decreased"
              ? "轮次持平，工具调用 / Token 改善"
              : decisionPolicy.decision_basis === "secondary_metrics_increased"
              ? "轮次持平，工具调用 / Token 有增加"
              : "三项持平 / 数据不完整"}
          </Pill>
        </div>

        {Object.keys(efficiencyDimensions).length > 0 && (
          <>
            <SecTitle>⚡ A/B 效率对比</SecTitle>
            <div
              role="region"
              aria-label={`A/B 效率对比：轮次 基线 ${Number(efficiencyDimensions.interaction_turns?.baseline || 0).toLocaleString()} / 候选 ${Number(efficiencyDimensions.interaction_turns?.candidate || 0).toLocaleString()}，工具 基线 ${Number(efficiencyDimensions.tool_call_count?.baseline || 0).toLocaleString()} / 候选 ${Number(efficiencyDimensions.tool_call_count?.candidate || 0).toLocaleString()}，Token 基线 ${Number(efficiencyDimensions.total_tokens?.baseline || 0).toLocaleString()} / 候选 ${Number(efficiencyDimensions.total_tokens?.candidate || 0).toLocaleString()}`}
              className="overflow-hidden rounded-lg border border-border"
            >
              <table className="w-full border-collapse text-xs">
                <thead>
                  <tr className="bg-surface-subtle text-muted-foreground">
                    {["指标", "基线", "候选", "变化", "结果"].map((label) => (
                      <th key={label} className="px-3 py-2 text-left font-semibold">{label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(efficiencyDimensions).map(([key, metric]) => (
                    <tr key={key} className="border-t border-border">
                      <td className="px-3 py-2 font-semibold">{EFFICIENCY_LABELS[key] || key}</td>
                      <td className="px-3 py-2">{Number(metric.baseline || 0).toLocaleString()}</td>
                      <td className="px-3 py-2">{Number(metric.candidate || 0).toLocaleString()}</td>
                      <td className={cn("px-3 py-2 font-bold", metric.delta > 0 && "text-success", metric.delta < 0 && "text-destructive")}>
                        {metric.delta > 0
                          ? `减少 ${Number(metric.delta).toLocaleString()}`
                          : metric.delta < 0
                          ? `增加 ${Math.abs(Number(metric.delta)).toLocaleString()}`
                          : "持平"}
                      </td>
                      <td className="px-3 py-2">
                        <Pill tone={metric.winner === "candidate" ? "green" : metric.winner === "baseline" ? "red" : "gray"}>
                          {metric.winner === "candidate" ? "减少" : metric.winner === "baseline" ? "增加" : "持平"}
                        </Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <SecTitle>技能包变更</SecTitle>
        {(bundleDiff || staticValidation) && (
          <div className="mb-3 flex flex-wrap gap-2">
            {bundleDiff && (
              <Pill tone="blue">变更 {bundleDiff.changed_count || changedFiles.length} 个文件</Pill>
            )}
            {staticValidation && (
              <Pill tone={staticValidation.passed ? "green" : "red"}>
                静态检查{staticValidation.passed ? "通过" : "失败"}
              </Pill>
            )}
          </div>
        )}
        {staticValidation?.errors?.length ? (
          <div className="mb-3 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
            {staticValidation.errors.join("\n")}
          </div>
        ) : null}
        {changedFiles.length > 0 && (
          <div className="mb-3 space-y-2">
            {changedFiles.map((file) => (
              <details key={file.path} className="rounded-lg border border-border p-3">
                <summary className="flex cursor-pointer items-center gap-2 text-xs font-semibold">
                  <Pill tone={file.status === "deleted" ? "red" : file.status === "added" ? "green" : "blue"}>
                    {file.status}
                  </Pill>
                  <span className="mono">{file.path}</span>
                </summary>
                {file.diff ? (
                    <UnifiedDiffView diff={file.diff} className="mt-2 max-h-[360px]" />
                ) : (
                  <div className="mt-2 text-xs text-muted-foreground">
                    二进制文件或无可展示文本差异，{file.old_size || 0} → {file.new_size || 0} bytes
                  </div>
                )}
              </details>
            ))}
          </div>
        )}
        <div className="grid gap-2.5 sm:grid-cols-2">
          <SkillMdBlock title="当前 SKILL.md" body={currentMd || missingCurrentText} />
          <SkillMdBlock title="候选 SKILL.md" body={candidateMd || "（无候选内容）"} />
        </div>
        {skillDiff && (
          <details className="rounded-lg border border-border p-3">
            <summary className="cursor-pointer text-xs font-semibold">查看 Unified Diff</summary>
              <UnifiedDiffView diff={skillDiff} className="mt-2 max-h-[360px]" />
          </details>
        )}
        <SecTitle>🔁 A/B 回放明细（基线 vs 候选）</SecTitle>
        {replayHtml}

        <div className="mt-3.5 flex items-center gap-2">
          {!readOnly && (
            <Button
              variant="outline"
              size="sm"
              disabled={evaluating}
              onClick={() => onEvaluate(true)}
            >
              {evaluating ? "评估中…" : "重新评估（重跑回放）"}
            </Button>
          )}
          <span className="text-xs text-muted-foreground">
            {readOnly ? "历史候选只读展示" : ev.cached ? "结果来自缓存" : "本次实时评估"}
          </span>
        </div>
      </div>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[88vh] w-full !max-w-[860px] overflow-auto">
        <DialogHeader>
          <DialogTitle>评估详情 · {candidateName(cand, ev, jobId)}</DialogTitle>
        </DialogHeader>
        {bodyInner}
      </DialogContent>
    </Dialog>
  );
}

function SkillMdBlock({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface-subtle p-3">
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{title}</div>
      <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap text-[11px]">{body}</pre>
    </div>
  );
}

function AbCol({
  head,
  branch,
  body,
}: {
  head: string;
  body?: string;
  branch: ReplaySide;
}) {
  return (
    <div className="rounded-md border border-border bg-surface-subtle px-2.5 py-2.5">
      <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">
        {head}
      </div>
      <div className="mb-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
        <span>轮次 {Number(branch.interaction_turns || 0).toLocaleString()}</span>
        <span>工具 {Number(branch.tool_call_count || 0).toLocaleString()}</span>
        <span>Token {Number(branch.total_tokens || 0).toLocaleString()}</span>
      </div>
      <div className="max-h-[200px] overflow-auto text-xs leading-normal whitespace-pre-wrap break-words">
        {body || <span className="text-muted-foreground">（无输出）</span>}
      </div>
    </div>
  );
}
