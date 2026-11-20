import { api } from "@/api/client";
import { useEffect, useState } from "react";

type Status = "running" | "completed" | "cancelled" | "failed" | "incomplete";
type Progress = {
  status: Status;
  complete: boolean;
  tracking_available: boolean;
  counts: { fetched: number; enqueued: number; duplicate: number; error: number };
  summary: { leaf: { archived: number; skipped: number; pending: number; failed: number; cancelled: number } };
};

const labels: Record<Status, string> = {
  running: "正在拉取或处理", completed: "处理完成", cancelled: "已取消",
  failed: "存在失败项", incomplete: "记录不完整",
};

export function PullProgressPanel({ jobId }: { jobId: string }) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number;
    async function refresh() {
      try {
        const result = await api<Progress>(`/api/datasource/jobs/${encodeURIComponent(jobId)}/progress?limit=1`);
        if (cancelled) return;
        setProgress(result);
        setError("");
        if (result.complete || !result.tracking_available || result.status === "incomplete") return;
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "暂时无法读取本次进度");
      }
      timer = window.setTimeout(refresh, 3000);
    }
    void refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [jobId]);
  const counts = progress?.counts;
  const leaf = progress?.summary?.leaf;
  return (
    <div className="space-y-1 text-xs" aria-live="polite">
      <p>本次拉取 <code>{jobId}</code> · {progress ? labels[progress.status] : "正在拉取进度"}</p>
      {counts && <p>已读取 {counts.fetched} · 新入队 {counts.enqueued} · 重复 {counts.duplicate} · 读取失败 {counts.error}</p>}
      {progress?.tracking_available && leaf && (
        <p>已归档 {leaf.archived} · 已跳过 {leaf.skipped} · 处理中 {leaf.pending} · 失败 {leaf.failed} · 已取消 {leaf.cancelled}</p>
      )}
      {progress && !progress.tracking_available && <p>这次拉取缺少逐条归档记录。</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
