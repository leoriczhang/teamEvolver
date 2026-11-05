import { Pill } from "@/components/common";
import type { MARelease } from "./api/client";

export function maTime(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚无记录";
}
export default function ReleaseHistory({ releases }: { releases: MARelease[] }) {
  return <div className="space-y-3">
    {!releases.length && <p className="text-sm text-muted-foreground">首轮资源发现完成后生成发布快照。</p>}
    {releases.map(release => <article key={release.release_id} className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-xs">{release.release_id}</span>
        <Pill tone={release.status === "applied" ? "green" : release.status === "drift" ? "red" : "amber"}>
          {release.status === "applied" ? "已完整应用" : release.status === "pending" ? "待应用／恢复中" : release.status}
        </Pill>
      </div>
      <div className="mt-3 grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <span>入口 Agent v{release.record.entry_agent_version || "—"}</span>
        <span>Environment {release.record.environment_id || "—"}</span>
        <span>受理时间 {maTime(release.created_at)}</span><span>应用时间 {maTime(release.applied_at)}</span>
      </div>
      {release.record.runtime_tree_sha256 && <p className="mt-2 break-all font-mono text-xs">
        Runtime {release.record.runtime_tree_sha256}</p>}
      {(release.record.error || release.record.drift) && <p className="mt-2 text-xs text-destructive">
        {release.record.error || release.record.drift}</p>}
    </article>)}
  </div>;
}
