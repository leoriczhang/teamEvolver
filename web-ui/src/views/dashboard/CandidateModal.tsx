import type { ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Pill, Empty, ListViewport, PaginationControls, usePagedItems } from "@/components/common";
import { cn } from "@/lib/utils";
import { fmtScore } from "@/lib/format";
import type { Candidate, EvalResult } from "@/api/client";

const VERIFY_LABELS: Record<string, string> = {
  grounded_in_evidence: "有据可依（基于会话证据）",
  preserves_existing_value: "保留既有价值（不破坏原技能）",
  specificity_and_reusability: "具体且可复用",
  safe_to_publish: "可安全发布",
};

const EFFICIENCY_LABELS: Record<string, string> = {
  interaction_turns: "交互轮次",
  tool_call_count: "工具调用",
  total_tokens: "Tokens",
};

function bar(v?: number | null) {
  const pct = v == null || isNaN(Number(v)) ? 0 : Math.max(0, Math.min(1, Number(v))) * 100;
  const cls = v == null ? "" : Number(v) >= 0.75 ? "good" : Number(v) < 0.5 ? "bad" : "";
  return (
    <div className="bar">
      <span className={cls} style={{ width: `${pct.toFixed(0)}%` }} />
    </div>
  );
}

function kv(v?: number | null, thr?: number | null): { cls: string; txt: string } {
  if (v == null || isNaN(Number(v))) return { cls: "muted", txt: "—" };
  return { cls: thr != null ? (Number(v) >= thr ? "good" : "bad") : "", txt: fmtScore(v) };
}

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

function Kpi({
  label,
  value,
  cls,
  tip,
}: {
  label: string;
  value: string;
  cls: string;
  tip: ReactNode;
}) {
  return (
    <div className="min-w-[120px] flex-1 rounded-lg border border-border bg-surface-subtle px-3 py-2.5">
      <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">{label}</div>
      <div
        className={cn(
          "text-[19px] font-bold",
          cls === "good" && "text-success",
          cls === "bad" && "text-destructive",
          cls === "muted" && "font-normal text-muted-foreground"
        )}
      >
        {value}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">{tip}</div>
    </div>
  );
}

export default function CandidateModal({
  jobId,
  cand,
  ev,
  evaluating,
  open,
  onClose,
  onEvaluate,
}: {
  jobId: string | null;
  cand: Candidate | null;
  ev: EvalResult | null;
  evaluating: boolean;
  open: boolean;
  onClose: () => void;
  onEvaluate: (force: boolean) => void;
}) {
  const rep = ev?.replay || {};
  const ver = ev?.verification || {};
  const replayCases = rep.cases || [];
  const efficiencyDimensions = rep.efficiency?.dimensions || {};
  const replayPager = usePagedItems(replayCases);
  const currentMd = ev?.current_skill_md || cand?.current_skill_md || skillToMd(cand?.current_skill || ev?.current_skill);
  const candidateMd = ev?.candidate_skill_md || cand?.candidate_skill_md || skillToMd(cand?.candidate_skill || ev?.candidate_skill);
  const skillDiff = ev?.skill_diff || cand?.skill_diff || "";
  const thr =
    cand?.min_score != null
      ? cand.min_score
      : rep.threshold != null
        ? rep.threshold
        : 0.75;

  let bodyInner: ReactNode;
  if (!ev) {
    bodyInner = evaluating ? (
      <Empty>正在运行 Verify + A/B 回放评估，请稍候…（首次评估需调用模型，可能耗时较久）</Empty>
    ) : (
      <Empty>
        尚未评估。{" "}
        <Button variant="outline" size="sm" onClick={() => onEvaluate(false)}>
          开始评估
        </Button>
      </Empty>
    );
  } else {
    const kVer = kv(ev.verify_score, ver.threshold);
    const kRep = kv(ev.replay_score, rep.threshold != null ? rep.threshold : thr);
    const kBase = kv(rep.baseline_mean, null);

    // Verify detail
    let verifyHtml: ReactNode;
    if (ver.enabled === false) {
      verifyHtml = (
        <div className="text-xs text-muted-foreground">
          Verify 校验未启用（服务未开启 skill verifier）。
        </div>
      );
    } else if (ver.error) {
      verifyHtml = <div className="text-xs text-destructive">验证失败：{ver.error}</div>;
    } else {
      const checks = ver.checks || {};
      const keys = Object.keys(VERIFY_LABELS)
        .filter((k) => checks[k] != null)
        .concat(Object.keys(checks).filter((k) => !(k in VERIFY_LABELS)));
      const decision = ver.decision ? (
        <Pill tone={ver.accepted ? "green" : "red"}>
          {ver.decision === "accept" ? "接受" : "拒绝"}
        </Pill>
      ) : null;
      verifyHtml = (
        <>
          {keys.length ? (
            keys.map((k) => {
              const v = checks[k];
              return (
                <div key={k} className="my-1.5 flex items-center gap-2.5 text-xs">
                  <div className="w-[210px] shrink-0">{VERIFY_LABELS[k] || k}</div>
                  {bar(v)}
                  <div className="w-[46px] shrink-0 text-right font-bold">
                    {v == null ? "—" : Number(v).toFixed(2)}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="text-xs text-muted-foreground">本次评估未返回细分检查项。</div>
          )}
          {ver.reason ? (
            <div className="mt-2.5">
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                评审理由 {decision}
              </div>
              <div className="text-sm">{ver.reason}</div>
            </div>
          ) : (
            decision && <div className="mt-2">{decision}</div>
          )}
        </>
      );
    }

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
          const bScore = b.score;
          const aScore = a.score;
          const aWin = aScore != null && bScore != null && aScore >= bScore;
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
                  win={!aWin}
                  head="🅰 基线（当前技能 / 无技能）"
                  score={bScore}
                  scoreCls={kv(bScore, thr).cls}
                  body={b.response}
                />
                <AbCol
                  win={aWin}
                  head="🅱 候选（新技能）"
                  score={aScore}
                  scoreCls={kv(aScore, thr).cls}
                  body={a.response}
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
            <Pill tone="blue">{ev.proposed_action || cand?.proposed_action || "-"}</Pill>
          </div>
        </div>
        {cand?.rationale && (
          <div>
            <div className="mb-1.5 text-xs font-semibold text-muted-foreground">进化理由</div>
            <div>{cand.rationale}</div>
          </div>
        )}

        {/* KPIs */}
        <div className="my-4 flex flex-wrap gap-2.5">
          <Kpi
            label="验证分 (Verify)"
            value={kVer.txt}
            cls={kVer.cls}
            tip={
              <>
                门槛 {ver.threshold != null ? Number(ver.threshold).toFixed(2) : "—"}
                {ver.enabled === false ? " · 未启用" : ""}
              </>
            }
          />
          <Kpi
            label="A/B 回放分（候选）"
            value={kRep.txt}
            cls={kRep.cls}
            tip={<>门槛 {Number(rep.threshold != null ? rep.threshold : thr).toFixed(2)}</>}
          />
          <Kpi
            label="基线分"
            value={kBase.txt}
            cls={kBase.cls}
            tip={<>候选需 ≥ 基线−{rep.tolerance != null ? Number(rep.tolerance).toFixed(2) : "0.15"}</>}
          />
          <Kpi
            label="综合建议"
            value={ev.recommended_publish ? "建议发布" : "建议复核"}
            cls={ev.recommended_publish ? "good" : "bad"}
            tip={
              <>
                {rep.no_regression ? "无回退" : "存在回退"} ·{" "}
                {ver.accepted === false
                  ? "验证未过"
                  : ver.enabled === false
                    ? "验证未启用"
                    : "验证通过"}
              </>
            }
          />
        </div>

        {Object.keys(efficiencyDimensions).length > 0 && (
          <>
            <SecTitle>⚡ A/B 效率对比</SecTitle>
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full border-collapse text-xs">
                <thead>
                  <tr className="bg-surface-subtle text-muted-foreground">
                    {["指标", "基线", "候选", "减少量", "结果"].map((label) => (
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
                        {metric.delta > 0 ? "-" : metric.delta < 0 ? "+" : ""}
                        {Math.abs(Number(metric.delta || 0)).toLocaleString()}
                      </td>
                      <td className="px-3 py-2">
                        <Pill tone={metric.winner === "candidate" ? "green" : metric.winner === "baseline" ? "red" : "gray"}>
                          {metric.winner === "candidate" ? "候选更优" : metric.winner === "baseline" ? "基线更优" : "持平"}
                        </Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <SecTitle>🛡 Verify 校验明细</SecTitle>
        {verifyHtml}
        <SecTitle>📄 Skill 变更内容</SecTitle>
        <div className="grid gap-2.5 sm:grid-cols-2">
          <SkillMdBlock title="当前 SKILL.md" body={currentMd || "（新建技能，无当前版本）"} />
          <SkillMdBlock title="候选 SKILL.md" body={candidateMd || "（无候选内容）"} />
        </div>
        {skillDiff && (
          <details className="rounded-lg border border-border p-3">
            <summary className="cursor-pointer text-xs font-semibold">查看 Unified Diff</summary>
            <pre className="mt-2 max-h-[360px] overflow-auto whitespace-pre-wrap text-[11px]">{skillDiff}</pre>
          </details>
        )}
        <SecTitle>🔁 A/B 回放明细（基线 vs 候选）</SecTitle>
        {replayHtml}

        <div className="mt-3.5 flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={evaluating}
            onClick={() => onEvaluate(true)}
          >
            {evaluating ? "评估中…" : "重新评估（重跑回放）"}
          </Button>
          <span className="text-xs text-muted-foreground">
            {ev.cached ? "结果来自缓存" : "本次实时评估"}
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
  win,
  head,
  score,
  scoreCls,
  body,
}: {
  win: boolean;
  head: string;
  score?: number | null;
  scoreCls: string;
  body?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-md border bg-surface-subtle px-2.5 py-2.5",
        win ? "border-success" : "border-border"
      )}
    >
      <div className="mb-1.5 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{head}</span>
        <span
          className={cn(
            "font-bold",
            scoreCls === "good" && "text-success",
            scoreCls === "bad" && "text-destructive"
          )}
        >
          {score == null ? "—" : Number(score).toFixed(3)}
        </span>
      </div>
      <div className="max-h-[200px] overflow-auto text-xs leading-normal whitespace-pre-wrap break-words">
        {body || <span className="text-muted-foreground">（无输出）</span>}
      </div>
    </div>
  );
}
