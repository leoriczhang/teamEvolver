import { api } from "@/api/client";
import { Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useEffect,useRef,useState } from "react";

type ManualRun = {
  request_id: string;
  state: string;
  requested_at: number;
  finished_at?: number;
  error_code?: string;
};
type SyncStatus = {
  enabled: boolean;
  target_directory: string;
  counts: { pending: number; synced: number; retry: number; dead_letter?: number };
  index_pending?: number;
  retry?: { due: number; deferred: number; next_attempt_at: number | null };
  last_scan: number | null;
  last_error: string | null;
  manual: ManualRun | null;
};

const messages: Record<string, string> = {
  SYNC_DISABLED: "当前租户未开启经验同步，请先配置 experience_sync.enabled。",
  SYNC_ALREADY_RUNNING: "当前租户已有同步正在执行，请稍后查看状态。",
  OV_INDEX_NOT_READY: "正文已写入，语义或向量索引尚未完成。",
  OV_NETWORK_ERROR: "连接 OV 失败，后台将按策略重试。",
};
const explain = (code: string) => messages[code] || code;
const timeLabel = (value: number | null | undefined) =>
  value ? new Date(value * 1000).toLocaleString() : "尚无记录";

export default function ExperienceSyncPanel({ active }: { active: boolean }) {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState("");
  const [posting, setPosting] = useState(false);
  const epoch = useRef(0);
  const sequence = useRef(0);
  const submitting = useRef(false);

  async function load(generation: number) {
    if (submitting.current) return;
    const request = ++sequence.current;
    try {
      const result = await api<SyncStatus>("/api/experience-sync/status");
      if (epoch.current === generation && sequence.current === request) {
        setStatus(result);
        setError("");
      }
    } catch (err) {
      if (epoch.current === generation && sequence.current === request) {
        setError(explain(err instanceof Error ? err.message : "状态读取失败"));
      }
    }
  }

  useEffect(() => {
    const generation = ++epoch.current;
    setStatus(null);
    if (active) void load(generation);
    const timer = active ? window.setInterval(() => void load(generation), 5000) : undefined;
    return () => { ++epoch.current; window.clearInterval(timer); };
  }, [active]);

  async function trigger(operation: "sync" | "import_all" = "sync") {
    if (submitting.current || !active) return;
    submitting.current = true;
    ++sequence.current;
    const generation = epoch.current;
    setPosting(true);
    setError("");
    try {
      const path = operation === "import_all" ? "/api/experience-sync/import" : "/api/experience-sync/trigger";
      const receipt = await api<{ job_id: string; status_url: string }>(path, { method: "POST" });
      if (!receipt.job_id || receipt.status_url !== `/api/jobs/${receipt.job_id}`) {
        throw new Error("服务未返回有效的后台任务");
      }
      if (generation === epoch.current) setStatus(previous => previous && {
        ...previous, manual: { request_id: receipt.job_id, state: "queued", requested_at: Date.now() / 1000 },
      });
    } catch (err) {
      if (generation === epoch.current) setError(explain(err instanceof Error ? err.message : "触发失败"));
    } finally {
      submitting.current = false;
      setPosting(false);
    }
  }

  const running = ["queued", "running", "waiting_external", "reconciling"].includes(status?.manual?.state || "");
  return (
    <div className="mb-5">
      <Panel title="成功经验同步到 OV" extra={
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={!active || !status?.enabled || posting || running}
            onClick={() => void trigger("import_all")}>导入存量成功经验</Button>
          <Button size="sm" variant="outline" disabled={!active || !status?.enabled || posting || running}
            onClick={() => void trigger()}>
            {posting ? "提交中…" : running ? "同步处理中…" : "立即同步到 OV"}
          </Button>
        </div>
      }>
        <div className="space-y-2 p-4 text-xs leading-6 text-muted-foreground">
          <p>同步当前租户已保存的成功经验和归纳模式。仅新增或正文变化时上传，不受下方筛选条件影响。</p>
          <p>“导入存量成功经验”会核对全部已入库 Session 分析，补齐经验和同步记录，不调用模型。重复导入会去重。</p>
          {!status && !error && <p>正在读取同步状态…</p>}
          {status && <>
            <p className="break-all">目标目录：{status.target_directory}</p>
            {!status.enabled ? <p>{messages.SYNC_DISABLED}</p> : <>
              <p>待处理 {status.counts.pending} · 已同步 {status.counts.synced} · 等待重试 {status.counts.retry}
                {" · "}停止重试 {status.counts.dead_letter ?? 0}</p>
              <p>正文已写入、索引待确认：{status.index_pending ?? 0} 条</p>
              <p>最近扫描：{timeLabel(status.last_scan)}</p>
              {status.retry && status.counts.retry > 0 && <div>
                <p>上传重试：已到期 {status.retry.due} 条 · 等待退避 {status.retry.deferred} 条</p>
                <p>最早重试时间：{timeLabel(status.retry.next_attempt_at)}。实际执行受后台调度影响。</p>
              </div>}
              {(status.counts.dead_letter ?? 0) > 0 &&
                <p className="text-destructive">部分投递已停止重试，请检查最近同步错误。</p>}
              {running && <p role="status">已受理，后台处理中；此状态不代表上传完成。</p>}
              {status.manual?.state === "completed" && <p role="status">
                本轮核对结束：{timeLabel(status.manual.finished_at)}。请结合待处理、重试数量和错误判断同步结果。
              </p>}
              {["failed", "cancelled"].includes(status.manual?.state || "") &&
                <p role="status">本轮核对{status.manual?.state === "failed" ? "失败" : "已取消"}：
                  {status.manual?.error_code || status.manual?.request_id}</p>}
            </>}
            {status.last_error && <p className="text-destructive">最近同步错误：{explain(status.last_error)}</p>}
          </>}
          {error && <p role="alert" className="text-destructive">{error}</p>}
        </div>
      </Panel>
    </div>
  );
}
