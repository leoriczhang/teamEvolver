import { useEffect, useRef, useState } from "react";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import type { ExecutionProfile } from "@/features/skills/api/client";
import { fmtTime } from "@/lib/format";
import { toastErr } from "@/lib/toast";

const terminal = new Set(["succeeded", "failed", "cancelled"]);

export default function ExecutionReleasePanel({ profileId, isAdmin, onUpdated }: {
  profileId: string;
  isAdmin: boolean;
  onUpdated: (profile: ExecutionProfile) => void;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ExecutionProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmation, setConfirmation] = useState<{ release_id: string; expected_release_id: string } | null>(null);
  const [job, setJob] = useState<{ id: string; status: string; error_code?: string } | null>(null);
  const sequence = useRef(0);
  const endpoint = `/api/skill-evolution/execution-profiles/${encodeURIComponent(profileId)}`;
  const pending = Boolean(job && !terminal.has(job.status));

  useEffect(() => () => { sequence.current++; }, [profileId]);

  async function refresh() {
    const request = ++sequence.current;
    setLoading(true);
    setConfirmation(null);
    try {
      const result = await api<ExecutionProfile>(endpoint);
      if (sequence.current !== request) return;
      if (result.profile_id !== profileId) throw new Error("执行配置详情不匹配");
      setDetail(result);
      onUpdated(result);
      if (job && !terminal.has(job.status)) {
        const status = await api<{ status: string; error_code?: string }>(`/api/jobs/${encodeURIComponent(job.id)}`);
        if (sequence.current !== request) return;
        setJob({ id: job.id, ...status });
      }
      setError("");
    } catch (cause: any) {
      if (sequence.current === request) setError(cause.message || "发布与回执加载失败");
    } finally {
      if (sequence.current === request) setLoading(false);
    }
  }

  async function rollback() {
    if (!confirmation || !isAdmin || saving || pending) return;
    setSaving(true);
    try {
      const accepted = await api<{ job_id: string }>(`${endpoint}/rollback`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(confirmation),
      });
      if (!accepted.job_id) throw new Error("服务未返回后台任务 ID");
      setJob({ id: accepted.job_id, status: "queued" });
      setConfirmation(null);
    } catch (cause: any) {
      toastErr("提交组合回滚失败", cause.message);
    } finally {
      setSaving(false);
    }
  }

  return <section aria-label="业务组合发布与 Agent 回执" className="space-y-3 border-t border-border pt-3 text-xs">
    <Button size="sm" variant="outline" disabled={loading || saving} onClick={() => {
      setOpen(true);
      void refresh();
    }}>{loading ? "加载中…" : open ? "刷新发布与 Agent 回执" : "查看发布与 Agent 回执"}</Button>
    {open && <>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {job && <div role="status" className="break-all rounded-md bg-muted p-2">
        回滚任务：{job.id} · {job.status}{job.error_code ? ` · ${job.error_code}` : ""}
        {pending && <p className="mt-1 text-muted-foreground">任务在后台执行；点击刷新查看任务与当前组合，受理成功不代表已经生效。</p>}
      </div>}
      {detail && <>
        <div className="break-all">当前组合发布：{detail.current_release_id}</div>
        <div className="space-y-2">
          <h4 className="font-semibold">发布记录</h4>
          {!detail.releases?.length && <p className="text-muted-foreground">暂无发布记录。</p>}
          {(detail.releases || []).map(release => <div key={release.release_id} className="space-y-1 rounded-md bg-muted p-2">
            <div className="break-all">{release.release_id}{release.release_id === detail.current_release_id ? "（当前）" : ""}</div>
            <div className="break-all text-muted-foreground">组合哈希：{release.composition_hash}</div>
            {release.created_at && <div className="text-muted-foreground">{fmtTime(release.created_at)}</div>}
            {release.restored_from && <div className="break-all text-muted-foreground">回滚来源：{release.restored_from}</div>}
            {isAdmin && release.release_id !== detail.current_release_id && <Button size="sm" variant="outline"
              disabled={saving || loading || pending} onClick={() => setConfirmation({
                release_id: release.release_id, expected_release_id: detail.current_release_id,
              })}>回滚到此组合 · {release.release_id}</Button>}
          </div>)}
        </div>
        {confirmation && <div className="space-y-2 rounded-md border border-border p-3">
          <p className="break-all">将整套 Skill／业务 Prompt／上下文组合恢复到：{confirmation.release_id}</p>
          <p className="break-all text-muted-foreground">当前发布检查：{confirmation.expected_release_id}。期间组合发生变化时服务将拒绝回滚。</p>
          <div className="flex gap-2">
            <Button size="sm" disabled={saving || pending} onClick={rollback}>{saving ? "提交中…" : "确认整套组合回滚"}</Button>
            <Button size="sm" variant="outline" disabled={saving} onClick={() => setConfirmation(null)}>取消回滚</Button>
          </div>
        </div>}
        <div className="space-y-2">
          <h4 className="font-semibold">Agent 下载／激活回执</h4>
          <p className="text-muted-foreground">证据级别：agent_reported（Agent 自报）。下载或激活回执不证明模型已经实际使用这些内容。</p>
          {!detail.receipts?.length && <p className="text-muted-foreground">暂无 Agent 下载或激活回执。</p>}
          {(detail.receipts || []).map((receipt, index) => <div key={receipt.request_id || index} className="rounded-md bg-muted p-2">
            <div>{receipt.state === "downloaded" ? "已下载" : "已激活"} · agent_reported{receipt.at ? ` · ${fmtTime(receipt.at)}` : ""}</div>
            <div className="break-all">发布：{receipt.release_id}</div>
            <div className="break-all text-muted-foreground">组合哈希：{receipt.composition_hash}</div>
          </div>)}
        </div>
      </>}
    </>}
  </section>;
}
