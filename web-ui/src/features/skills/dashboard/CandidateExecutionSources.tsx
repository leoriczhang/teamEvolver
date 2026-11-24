import type { Candidate } from "@/features/skills/api/client";

export default function CandidateExecutionSources({ sources }: {
  sources: Candidate["execution_provenance"];
}) {
  if (!sources?.length) return null;
  return (
    <details className="rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-xs font-semibold">
        进化来源 · {sources.length} 份执行快照
      </summary>
      <div className="mt-3 max-h-72 space-y-3 overflow-y-auto text-xs">
        {sources.map((source, index) => (
          <div key={`${source.session_id}-${index}`} className="space-y-1 break-all border-t border-border pt-2">
            <div className="font-mono text-muted-foreground">{source.session_id}</div>
            <div>业务时间：{source.business_time || "未记录"} · 入库时间：{source.ingested_at || "未记录"}</div>
            {source.agents?.map((agent) => (
              <div key={agent.id}>
                Agent：{agent.id} · v{agent.version ?? "?"} · 模型：{agent.model?.id || "未记录"}
                {agent.model?.base_url && <span className="ml-2 text-muted-foreground">{agent.model.base_url}</span>}
              </div>
            ))}
            <div>环境：{source.environment?.name || "未记录"} · {source.environment?.id || "未记录"}</div>
            {source.execution_fingerprint?.environment_hash && (
              <div className="font-mono text-muted-foreground">
                环境配置哈希：{source.execution_fingerprint.environment_hash}
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
