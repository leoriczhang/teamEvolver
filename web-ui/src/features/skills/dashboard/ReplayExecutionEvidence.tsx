import type { ReplaySide } from "@/features/replay/api/client";

export default function ReplayExecutionEvidence({ branch }: { branch: ReplaySide }) {
  const records = branch.observed_requests || [];
  const verification = branch.execution_verification;
  if ((!verification || verification === "not_required") && !records.length) return null;
  return <details className="mt-3 rounded-md border border-border p-2 text-[11px]">
    <summary className="cursor-pointer font-semibold">最终模型请求加载证据 · {verification || "未记录"}</summary>
    <div className="mt-2 space-y-2">
      <div className="break-all">执行计划哈希：{branch.execution_plan_hash || "未记录"}</div>
      <p className="text-muted-foreground">此处展示 Replay 最终请求派发与核验结果。Agent 下载／激活自报不等同于模型实际使用。</p>
      {!records.length && <p className="text-destructive">没有完整派发记录，不能仅凭状态或哈希确认实际加载。</p>}
      {records.map((record, index) => <div key={record.dispatch_id || index} className="space-y-1 rounded bg-muted p-2">
        {record.observation === "remote_attested_dispatch" && <div>证据来源：远端模型传输签名核验（remote_attested_dispatch）</div>}
        {record.runtime_fingerprint != null && <div className="break-all">Runtime 指纹：{JSON.stringify(record.runtime_fingerprint)}</div>}
        <div className="break-all">派发 ID：{record.dispatch_id || "未记录"}</div>
        <div className="break-all">请求哈希：{record.request_hash || "未记录"}</div>
        <div className="break-all">目的地址：{JSON.stringify(record.destination || {})}</div>
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(record.request || {}, null, 2)}</pre>
      </div>)}
    </div>
  </details>;
}
