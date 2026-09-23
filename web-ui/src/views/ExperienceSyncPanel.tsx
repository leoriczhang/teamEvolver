import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { Panel } from "@/components/common";
import { Button } from "@/components/ui/button";

type ManualRun = {
  request_id: string;
  state: "queued" | "running" | "completed";
  requested_at: number;
  finished_at?: number;
  operation?: "sync" | "import_all";
  import?: {
    phase: "objects" | "index" | "prepare" | "complete";
    processed_sources: number;
    eligible_records: number;
    prepared_documents: number;
    rejected_sources: number;
    rejected_records?: number;
    error_counts?: Record<string, number>;
    errors_sample?: { source_key: string; session_id?: string; code: string;
      source_bytes?: number; record_bytes?: number; limit_bytes?: number; ordinal?: number }[];
    last_error: string | null;
  };
};
type SyncStatus = {
  enabled: boolean;
  target_directory: string;
  counts: { pending: number; synced: number; retry: number };
  index_pending?: number;
  last_error_at?: number | null;
  last_delivery_pass?: { finished_at: number; attempted: number; deferred: number } | null;
  worker?: { state: string; scheduler_running: boolean; error?: string; registry_error?: string };
  retry?: { due: number; deferred: number; next_attempt_at: number | null; last_attempt_at: number | null;
    error_counts: Record<string, number>;
    samples: { uri: string; code: string; attempts: number; next_attempt_at: number;
      failure?: { stage?: string; path?: string; status?: number; ov_request_id?: string } | null }[] };
  last_scan: number | null;
  last_error: string | null;
  manual: ManualRun | null;
};

const messages: Record<string, string> = {
  SYNC_DISABLED: "当前租户未开启经验同步，请先配置 experience_sync.enabled。",
  SYNC_ALREADY_RUNNING: "当前租户已有同步正在执行，请稍后查看状态。",
  SYNC_OTHER_OPERATION_RUNNING: "当前租户已有导入或同步请求，请等待本轮结束。",
  SYNC_BUSY: "同步请求较多，请稍后重试。",
  SYNC_NOT_RUNNING: "后台同步器未启动，请检查 PostgreSQL 配置和服务日志。",
  SYNC_STORAGE_FAILURE: "同步状态存储不可用，请检查服务日志。",
  IMPORT_SOURCE_TOO_LARGE: "来源超过配置的原始大小预算",
  IMPORT_RECORD_TOO_LARGE: "单条经验超过 256 KiB 投影预算",
  IMPORT_SOURCE_CHANGED: "读取期间来源已变化，请再次导入",
  IMPORT_QUERY_TIMEOUT: "来源读取超时，请检查 PG 后重试",
  INVALID_IMPORT_SOURCE: "来源 JSON 或经验数组格式无效",
  INVALID_IMPORT_RECORD: "单条经验字段不完整或格式无效",
  OV_PATH_TYPE_CONFLICT: "OV 目标路径类型冲突，请检查同名文件或目录",
  OV_INVALID_STAT: "OV 路径状态响应不完整，尚未确认目录可用",
  DEADLINE_EXCEEDED: "DEADLINE_EXCEEDED：OV 等待处理超时；正文和索引状态需分别核对，请检查 OV 队列及模型日志",
  OV_TIMEOUT: "OV_TIMEOUT：连接 OV 超时，写入结果待核对",
  OV_INDEX_NOT_READY: "OV_INDEX_NOT_READY：正文已写入，语义或向量索引尚未确认完成",
};
const explain = (code: string) => messages[code] || code;
const timeLabel = (value: number | null | undefined) => value ? new Date(value * 1000).toLocaleString() : "尚无记录";

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
      const receipt = await api<{ manual: ManualRun }>(path, { method: "POST" });
      if (generation === epoch.current) setStatus(previous => previous && { ...previous, manual: receipt.manual });
    } catch (err) {
      if (generation === epoch.current) setError(explain(err instanceof Error ? err.message : "触发失败"));
    } finally {
      submitting.current = false;
      setPosting(false);
    }
  }

  const running = status?.manual?.state === "queued" || status?.manual?.state === "running";
  return (
    <div className="mb-5">
      <Panel title="成功经验同步到 OV" extra={
        <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={!active || !status?.enabled || posting || running}
          onClick={() => void trigger("import_all")}>
          导入存量成功经验
        </Button>
        <Button size="sm" variant="outline" disabled={!active || !status?.enabled || posting || running}
          onClick={() => void trigger()}>
          {posting ? "提交中…" : running ? "同步处理中…" : "立即同步到 OV"}
        </Button>
        </div>
      }>
        <div className="space-y-2 p-4 text-xs leading-6 text-muted-foreground">
          <p>核对当前租户已保存到经验 JSON 的全部成功经验，不受下方筛选条件影响。仅新增或正文变化时上传；失败项按原重试间隔恢复。</p>
          <p>“导入存量成功经验”还会读取当前租户的全部 Session 分析和历史经验，补齐到 OV；不受下方筛选或分页限制，不调用模型。重复导入会去重，无可导入条目时不会上传文件。</p>
          {!status && !error && <p>正在读取同步状态…</p>}
          {status && <>
            <p className="break-all">目标目录：{status.target_directory}</p>
            {!status.enabled ? <p>{messages.SYNC_DISABLED}</p> : <>
              <p>待处理 {status.counts.pending} · 已同步 {status.counts.synced} · 等待重试 {status.counts.retry}</p>
              <p>正文已写入、索引待确认：{status.index_pending ?? 0} 条（包含在上述待处理或重试状态内）</p>
              <p>最近扫描：{timeLabel(status.last_scan)}</p>
              {status.worker && <p>后台执行状态（当前实例）：{{ running: "正在执行", idle: "等待下一轮", blocked: "执行受阻", lock_busy: "租户锁被其他执行者持有", stopped: "调度器未运行", waiting: "等待调度" }[status.worker.state] || status.worker.state}
                {status.worker.error ? ` · ${explain(status.worker.error)}` : ""}
                {status.worker.registry_error ? ` · 租户调度异常：${status.worker.registry_error}` : ""}</p>}
              {status.last_delivery_pass && <p>最近上传检查：{timeLabel(status.last_delivery_pass.finished_at)} · 本批尝试 {status.last_delivery_pass.attempted} 条 · 退避跳过 {status.last_delivery_pass.deferred} 条</p>}
              {status.retry && status.counts.retry > 0 && <div className="space-y-1 rounded border border-line p-3">
                <p>上传重试：已到期 {status.retry.due} 条 · 等待退避 {status.retry.deferred} 条</p>
                <p>最近失败尝试：{timeLabel(status.retry.last_attempt_at)} · 最早重试时间：{timeLabel(status.retry.next_attempt_at)}</p>
                <p>重试时间是最早可执行时间，实际执行受后台调度和租户锁影响。重新导入相同正文不会重置退避。</p>
                {Object.entries(status.retry.error_counts).map(([code, count]) => <p key={code}>{code}：{count} 条</p>)}
                {status.retry.samples.map((item, index) => <p key={index} className="break-all">
                  {item.uri} · {item.code} · 累计失败 {item.attempts} 次
                  {item.failure ? ` · 阶段 ${item.failure.stage || "未知"} · ${item.failure.path || ""} · HTTP ${item.failure.status ?? "无响应"}${item.failure.ov_request_id ? ` · OV request ID ${item.failure.ov_request_id}` : ""}` : " · 历史记录暂无阶段明细，下次执行后补齐"}
                </p>)}
                <p>最多显示 5 条失败样例。若最早重试时间已过且上传检查持续不更新，请检查调度器或租户锁。</p>
              </div>}
              {running && <p role="status">已受理，后台处理中；此状态不代表上传完成。</p>}
              {status.manual?.state === "completed" && <p role="status">本轮核对结束{(status.manual.import?.rejected_sources ?? 0) > 0 ? "（部分完成）" : ""}：{timeLabel(status.manual.finished_at)}。请结合待处理、重试数量和错误判断同步结果。</p>}
              {status.manual?.import && <>
                <p>存量导入阶段：{{ objects: "扫描历史记录", index: "扫描 Session 索引", prepare: "准备同步文档", complete: "已交给同步器" }[status.manual.import.phase]}</p>
                <p>已扫描 {status.manual.import.processed_sources} 条来源 · 发现 {status.manual.import.eligible_records} 条成功记录 · 准备 {status.manual.import.prepared_documents} 条去重文档 · 跳过 {status.manual.import.rejected_sources} 条来源</p>
                {status.manual.import.last_error && <p className="text-destructive">存量导入存在缺口：{status.manual.import.last_error}。检查日志中的来源标识，修复后再次导入。</p>}
                {(status.manual.import.rejected_sources > 0) && <div className="space-y-1 rounded border border-line p-3">
                  <p className="font-semibold">来源读取缺口 · {status.manual.import.rejected_sources} 个来源 · {status.manual.import.rejected_records ?? 0} 条记录</p>
                  {Object.entries(status.manual.import.error_counts ?? {}).map(([code, count]) =>
                    <p key={code}>{explain(code)}：{count} 次</p>)}
                  {status.manual.import.errors_sample?.map((gap, index) => <p key={index} className="break-all">
                    {gap.source_key}{gap.session_id ? `（${gap.session_id}）` : ""} · {gap.code}
                    {gap.source_bytes != null ? ` · 来源 ${gap.source_bytes} 字节` : ""}
                    {gap.record_bytes != null ? ` · 记录 ${gap.record_bytes} 字节` : ""}
                    {gap.limit_bytes != null ? ` · 限额 ${gap.limit_bytes} 字节` : ""}
                    {gap.ordinal ? ` · 第 ${gap.ordinal} 条` : ""}
                  </p>)}
                  <p>最多显示 20 条缺口；完整记录保存在同步状态中。旧任务没有明细时，修复后重新导入可取得新报告。</p>
                </div>}
              </>}
            </>}
            {status.last_error && <p className="text-destructive">最近同步错误：{explain(status.last_error)}{status.last_error_at ? `（${timeLabel(status.last_error_at)}）` : ""}</p>}
          </>}
          {error && <p role="alert" className="text-destructive">{error}</p>}
        </div>
      </Panel>
    </div>
  );
}
