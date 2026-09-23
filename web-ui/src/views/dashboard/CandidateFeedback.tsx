import { useRef, useState } from "react";
import { api, type CandidateFeedback, type CandidateFeedbackField } from "@/api/client";
import { toastErr } from "@/lib/toast";

const LABELS: Record<CandidateFeedbackField, string> = {
  reviewed: "已审阅",
  adopted: "已采纳",
  rejected: "已驳回",
};

export default function CandidateFeedbackControls({
  jobId,
  feedback,
  readOnly = false,
  showHistory = false,
  onSaved,
}: {
  jobId: string;
  feedback?: CandidateFeedback;
  readOnly?: boolean;
  showHistory?: boolean;
  onSaved: (jobId: string, feedback: CandidateFeedback) => void;
}) {
  const [saving, setSaving] = useState(false);
  const inflight = useRef(false);

  async function update(field: CandidateFeedbackField, value: boolean) {
    if (inflight.current || readOnly || !feedback) return;
    inflight.current = true;
    setSaving(true);
    try {
      const result = await api<{ feedback: CandidateFeedback }>(
        `/api/skill-candidates/${encodeURIComponent(jobId)}/feedback`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value }),
        }
      );
      onSaved(jobId, result.feedback);
    } catch (error: any) {
      toastErr("保存标记失败", error.message);
    } finally {
      inflight.current = false;
      setSaving(false);
    }
  }

  return (
    <div className="space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {(Object.keys(LABELS) as CandidateFeedbackField[]).map((field) => {
          const manual = field === "reviewed";
          return (
            <label
              key={field}
              className={`flex items-center gap-1.5 whitespace-nowrap ${manual && !readOnly ? "cursor-pointer" : "cursor-default"}`}
              title={manual ? undefined : `${LABELS[field]}由候选操作自动标记`}
            >
              <input
                type="checkbox"
                className="size-3.5 accent-primary disabled:cursor-not-allowed"
                checked={feedback?.[field] ?? false}
                disabled={saving || readOnly || !feedback || !manual}
                onChange={(event) => void update(field, event.target.checked)}
              />
              {LABELS[field]}
            </label>
          );
        })}
        {saving && <span role="status" className="text-muted-foreground">保存中…</span>}
      </div>
      {showHistory && (
        <details className="rounded-md border border-border p-2.5">
          <summary className="cursor-pointer font-medium">操作记录（{feedback?.history.length ?? 0}）</summary>
          {feedback?.history.length ? (
            <ul className="mt-2 max-h-48 space-y-2 overflow-auto">
              {[...feedback.history].reverse().map((event, index) => (
                <li key={`${event.version}-${event.field}-${index}`} className="flex flex-wrap gap-x-2 gap-y-1">
                  <time dateTime={event.at} className="text-muted-foreground">
                    {new Date(event.at).toLocaleString("zh-CN", { hour12: false })}
                  </time>
                  <span>{event.actor_name || event.actor_id}</span>
                  <span>{event.value ? "勾选" : "取消勾选"}「{LABELS[event.field]}」</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="mt-2 text-muted-foreground">暂无操作记录</div>
          )}
        </details>
      )}
    </div>
  );
}
