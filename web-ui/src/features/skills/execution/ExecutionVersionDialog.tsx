import { useEffect, useRef, useState } from "react";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { ExecutionPlan, ExecutionProfile } from "@/features/skills/api/client";
import { fmtTime } from "@/lib/format";
import ExecutionVersionContent from "./ExecutionVersionContent";
import { ExecutionVersionMetrics, ExecutionVersionReceipts } from "./ExecutionVersionEvidence";
import { profileUrl, releaseUrl, type ExecutionReleaseDetail } from "./types";

type View = "content" | "diff" | "metrics" | "release";
const tabs: Array<{ id: View; label: string }> = [
  { id: "content", label: "Prompt 与上下文" }, { id: "diff", label: "版本差异" },
  { id: "metrics", label: "Replay 指标" }, { id: "release", label: "发布与 Agent 回执" },
];

function readableError(cause: unknown) {
  return cause instanceof Error ? cause.message : "版本详情加载失败";
}

export default function ExecutionVersionDialog({ profile, initialReleaseId, onReleaseChange, onClose, onManage }: {
  profile: ExecutionProfile;
  initialReleaseId?: string;
  onReleaseChange?: (releaseId: string) => void;
  onClose: () => void;
  onManage?: () => void;
}) {
  const [fullProfile, setFullProfile] = useState<ExecutionProfile | null>(null);
  const [releaseId, setReleaseId] = useState(initialReleaseId || profile.current_release_id);
  const [detail, setDetail] = useState<ExecutionReleaseDetail | null>(null);
  const [tab, setTab] = useState<View>("content");
  const [comparisonId, setComparisonId] = useState("frozen");
  const [comparison, setComparison] = useState<ExecutionPlan | null>(null);
  const [comparisonError, setComparisonError] = useState("");
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [profileLoading, setProfileLoading] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const profileSequence = useRef(0);

  useEffect(() => {
    const request = ++profileSequence.current;
    setProfileError("");
    setProfileLoading(true);
    void api<ExecutionProfile>(profileUrl(profile.profile_id)).then(result => {
      if (request !== profileSequence.current) return;
      if (result.profile_id !== profile.profile_id) throw new Error("执行配置详情不匹配");
      setFullProfile(result);
    }).catch((cause: unknown) => {
      if (request === profileSequence.current) setProfileError(readableError(cause));
    }).finally(() => {
      if (request === profileSequence.current) setProfileLoading(false);
    });
    return () => { profileSequence.current++; };
  }, [profile.profile_id, refreshKey]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setDetail(null);
    setComparisonId("frozen");
    setComparison(null);
    if (!releaseId) { setLoading(false); setError("此执行配置尚无发布版本"); return; }
    void api<ExecutionReleaseDetail>(releaseUrl(profile.profile_id, releaseId)).then(result => {
      if (cancelled) return;
      if (result.profile_id !== profile.profile_id || result.release.release_id !== releaseId) {
        throw new Error("返回的发布版本与所选版本不匹配");
      }
      setDetail(result);
    }).catch((cause: unknown) => {
      if (!cancelled) setError(readableError(cause));
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [profile.profile_id, releaseId, refreshKey]);

  useEffect(() => {
    let cancelled = false;
    setComparison(null);
    setComparisonError("");
    if (comparisonId === "frozen" || !detail) { setComparisonLoading(false); return; }
    setComparisonLoading(true);
    void api<ExecutionReleaseDetail>(releaseUrl(profile.profile_id, comparisonId)).then(result => {
      if (cancelled) return;
      if (result.profile_id !== profile.profile_id || result.release.release_id !== comparisonId) {
        throw new Error("对照发布版本不匹配");
      }
      setComparison(result.release.document.execution_plan);
    }).catch((cause: unknown) => {
      if (!cancelled) setComparisonError(readableError(cause));
    }).finally(() => { if (!cancelled) setComparisonLoading(false); });
    return () => { cancelled = true; };
  }, [profile.profile_id, comparisonId, detail]);

  const releases = fullProfile?.releases || [];
  const allReleases = releases.some(release => release.release_id === releaseId) ? releases
    : [{ release_id: releaseId, composition_hash: "" }, ...releases];
  const baseline = comparisonId === "frozen" ? detail?.validation.baseline_execution_plan : comparison;

  function selectRelease(id: string) {
    setReleaseId(id);
    onReleaseChange?.(id);
  }

  return <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
    <DialogContent className="flex max-h-[90vh] w-[min(1120px,calc(100vw-2rem))] max-w-none flex-col overflow-hidden sm:max-w-none">
      <DialogHeader className="shrink-0 pr-7">
        <DialogTitle>业务 Prompt 版本与指标</DialogTitle>
        <DialogDescription className="break-all text-xs">{profile.profile_id} · Agent：{profile.agent_id}</DialogDescription>
      </DialogHeader>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto overscroll-contain">
        <div className="shrink-0 space-y-3">
          <label className="block min-w-0 text-xs font-medium">查看发布版本
            <select className="mt-1 w-full min-w-0 rounded-md border border-border bg-background px-2 py-2 text-xs"
              value={releaseId} onChange={event => selectRelease(event.target.value)}>
              {allReleases.map(release => <option key={release.release_id} value={release.release_id}>
                {release.release_id === (detail?.current_release_id || fullProfile?.current_release_id || profile.current_release_id) ? "当前发布 · " : "历史发布 · "}
                {release.release_id}{release.created_at ? ` · ${fmtTime(release.created_at)}` : ""}
              </option>)}
            </select>
          </label>
          <nav aria-label="业务 Prompt 版本详情" className="flex flex-wrap gap-1 border-b border-border pb-2">
            {tabs.map(item => <Button key={item.id} size="sm" variant={tab === item.id ? "default" : "ghost"}
              aria-pressed={tab === item.id} onClick={() => setTab(item.id)}>{item.label}</Button>)}
          </nav>
        </div>
        {loading && <p role="status" className="py-8 text-center text-xs text-muted-foreground">正在读取所选发布版本…</p>}
        {error && <div role="alert" className="space-y-2 rounded-md border border-destructive/30 p-3 text-xs text-destructive">
          <p>{error}</p><Button size="sm" variant="outline" onClick={() => setRefreshKey(value => value + 1)}>重试版本详情</Button>
        </div>}
        {profileError && <div role="alert" className="space-y-2 rounded-md border border-destructive/30 p-3 text-xs text-destructive">
          <p>发布列表和 Agent 回执加载失败：{profileError}</p>
          <Button size="sm" variant="outline" onClick={() => setRefreshKey(value => value + 1)}>重试列表与回执</Button>
        </div>}
        {!loading && detail && <div className="min-w-0 space-y-4 pb-2">
          <div className="flex flex-wrap gap-x-4 gap-y-1 break-all text-[11px] text-muted-foreground">
            <span>{detail.release.release_id === detail.current_release_id ? "TE 当前发布" : "TE 历史发布"}</span>
            <span>发布时间：{fmtTime(detail.release.created_at)}</span>
            <span>Agent 是否采用：请查看回执与实际请求</span>
          </div>
          {tab === "diff" && <div className="space-y-2">
            <label className="block text-xs font-medium">正文对照来源
              <select className="mt-1 w-full min-w-0 rounded-md border border-border bg-background px-2 py-2 text-xs"
                value={comparisonId} onChange={event => setComparisonId(event.target.value)}>
                <option value="frozen">本次进化冻结基线{detail.validation.baseline_execution_plan ? "" : "（此版本未记录）"}</option>
                {releases.filter(release => release.release_id !== releaseId).map(release => <option key={release.release_id} value={release.release_id}>
                  历史发布 · {release.release_id}
                </option>)}
              </select>
            </label>
            <p className="text-[11px] text-muted-foreground">正文对比可切换历史版本；Replay 指标始终对应当次冻结的基线组合。</p>
            {comparisonLoading && <p role="status" className="text-xs text-muted-foreground">正在读取对照版本…</p>}
            {comparisonError && <p role="alert" className="text-xs text-destructive">{comparisonError}</p>}
          </div>}
          {(tab === "content" || tab === "diff") && (tab === "content" || (!comparisonLoading && !comparisonError)) && <ExecutionVersionContent
            plan={detail.release.document.execution_plan} baseline={baseline} showDiff={tab === "diff"} />}
          {tab === "metrics" && <ExecutionVersionMetrics detail={detail} onSelectRelease={selectRelease} />}
          {tab === "release" && <ExecutionVersionReceipts detail={detail} profile={fullProfile}
            receiptsStatus={profileLoading ? "loading" : profileError ? "failed" : "ready"} />}
        </div>}
      </div>
      <div className="flex shrink-0 flex-wrap justify-between gap-2 border-t border-border pt-3">
        {onManage ? <Button size="sm" variant="outline" onClick={() => { onClose(); onManage(); }}>登记与进化设置</Button> : <span />}
        <Button size="sm" variant="outline" onClick={onClose}>关闭版本详情</Button>
      </div>
    </DialogContent>
  </Dialog>;
}
