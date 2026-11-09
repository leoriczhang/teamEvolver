import { useState } from "react";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import type { Candidate, IndependentValidation } from "@/features/skills/api/client";
import { toastErr, toastOk } from "@/lib/toast";

const control = "mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs";
const statusNames = { passed: "通过", failed: "失败", inconclusive: "不确定" };

export default function IndependentValidationPanel({ candidate, readOnly, caseIds, onUpdated }: {
  candidate: Candidate;
  readOnly: boolean;
  caseIds: string[];
  onUpdated: (detail: Candidate) => void;
}) {
  const [status, setStatus] = useState<IndependentValidation["status"]>("inconclusive");
  const [method, setMethod] = useState<IndependentValidation["method"]>("domain_review");
  const [evidenceRef, setEvidenceRef] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const existing = candidate.independent_validation;
  const stale = existing && existing.candidate_revision !== candidate.candidate_revision;
  const canSubmit = !readOnly && candidate.review_status === "open" && !!candidate.candidate_revision
    && caseIds.length > 0 && evidenceRef.trim().length > 0 && evidenceRef.length <= 2000 && notes.length <= 8000;

  async function submit() {
    if (!canSubmit || saving) return;
    setSaving(true);
    try {
      const detail = await api<Candidate>(
        `/api/skill-candidates/${encodeURIComponent(candidate.job_id)}/independent-validation`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            candidate_revision: candidate.candidate_revision, status, method,
            evidence_ref: evidenceRef.trim(), case_ids: caseIds, notes,
          }),
        },
      );
      onUpdated(detail);
      toastOk("外部复核声明已登记", "声明绑定当前候选版本和全部当前测试用例");
    } catch (cause: any) {
      toastErr("登记外部复核失败", cause.message);
    } finally {
      setSaving(false);
    }
  }

  return <section className="space-y-3 rounded-lg border border-border p-3" aria-label="独立外部复核">
    <h3 className="text-xs font-semibold">独立外部复核</h3>
    <p className="text-[11px] text-muted-foreground">
      这是管理员依据领域审阅或程序校验提交的外部证据声明。系统保存声明与来源，并不据此证明 LLM 已自动判定业务事实正确。
      请先执行候选评估，再复核该次验证冻结的全部案例，包括共享回归与锁定验收用例。
    </p>
    <div className="text-xs text-muted-foreground">
      独立验证状态：{existing ? statusNames[existing.status] : "未提交"}{stale ? "（版本已过期）" : ""}
    </div>
    {existing && <div className="space-y-1 break-all text-xs text-muted-foreground">
      <div>复核方式：{existing.method === "programmatic" ? "程序校验" : "领域审阅"}</div>
      <div>证据引用：{existing.evidence_ref}</div>
      <div>复核说明：{existing.notes || "未填写"}</div>
      {existing.reviewer && <div>声明提交人：{existing.reviewer}</div>}
    </div>}
    {!readOnly && candidate.review_status === "open" && <details>
      <summary className="cursor-pointer text-xs font-semibold">登记外部复核声明</summary>
      <div className="mt-3 space-y-3">
        <label className="block text-xs font-semibold">外部复核结论
          <select className={control} value={status} disabled={saving}
            onChange={event => setStatus(event.target.value as IndependentValidation["status"])}>
            <option value="inconclusive">不确定</option><option value="passed">通过</option><option value="failed">失败</option>
          </select>
        </label>
        <label className="block text-xs font-semibold">外部复核方法
          <select className={control} value={method} disabled={saving}
            onChange={event => setMethod(event.target.value as IndependentValidation["method"])}>
            <option value="domain_review">领域审阅</option><option value="programmatic">程序校验</option>
          </select>
        </label>
        <label className="block text-xs font-semibold">外部证据引用
          <input className={control} value={evidenceRef} disabled={saving} maxLength={2000}
            placeholder="审阅记录、校验报告或可信产物引用"
            onChange={event => setEvidenceRef(event.target.value)} />
        </label>
        <label className="block text-xs font-semibold">外部复核说明
          <textarea className={control} value={notes} disabled={saving} maxLength={8000}
            onChange={event => setNotes(event.target.value)} />
        </label>
        <div className="text-xs text-muted-foreground">声明须覆盖全部当前用例（{caseIds.length}）</div>
        <div className="flex flex-wrap gap-2 text-[11px]">{caseIds.map(id => <span key={id} className="mono">{id}</span>)}</div>
        {!caseIds.length && <p role="alert" className="text-xs text-destructive">尚无可核验的当前用例 ID，请刷新候选详情后再登记。</p>}
        <Button size="sm" disabled={!canSubmit || saving} onClick={submit}>
          {saving ? "提交中…" : "提交外部复核声明"}
        </Button>
      </div>
    </details>}
  </section>;
}
