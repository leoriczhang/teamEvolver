import { useEffect, useRef, useState } from "react";
import { executionObjects } from "./variableGroups";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import type { ExecutionProfile } from "@/features/skills/api/client";
import ExecutionVersionDialog from "./ExecutionVersionDialog";
import { executionProfilesUrl } from "./types";

export default function ExecutionAssetsPanel({ active, onManage }: {
  active: boolean;
  onManage?: () => void;
}) {
  const [profiles, setProfiles] = useState<ExecutionProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<ExecutionProfile | null>(null);
  const [initialReleaseId, setInitialReleaseId] = useState("");
  const [linkError, setLinkError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    if (!active) return;
    const request = ++sequence.current;
    setLoading(true);
    setError("");
    void api<{ profiles: ExecutionProfile[] }>(executionProfilesUrl).then(result => {
      if (sequence.current !== request) return;
      const next = result.profiles || [];
      setProfiles(next);
      const params = new URL(window.location.href).searchParams;
      const requestedProfile = params.get("execution_profile");
      if (requestedProfile) {
        const match = next.find(profile => profile.profile_id === requestedProfile);
        if (match) {
          setInitialReleaseId(params.get("execution_release") || "");
          setSelected(match);
          setLinkError("");
        } else setLinkError(`未找到执行配置：${requestedProfile}，请检查当前租户与链接。`);
      }
    }).catch((cause: unknown) => {
      if (sequence.current === request) setError(cause instanceof Error ? cause.message : "业务 Prompt 加载失败");
    }).finally(() => {
      if (sequence.current === request) setLoading(false);
    });
    return () => { sequence.current++; };
  }, [active, refreshKey]);

  useEffect(() => { if (!active) setSelected(null); }, [active]);
  if (!active) return null;
  const filtered = profiles.filter(profile => [profile.profile_id, profile.agent_id, profile.user_id]
    .join(" ").toLowerCase().includes(query.trim().toLowerCase()));

  function updateLink(profileId?: string, releaseId?: string) {
    const url = new URL(window.location.href);
    if (profileId) url.searchParams.set("execution_profile", profileId);
    else url.searchParams.delete("execution_profile");
    if (releaseId) url.searchParams.set("execution_release", releaseId);
    else url.searchParams.delete("execution_release");
    window.history.replaceState(null, "", url);
  }

  return <section aria-label="业务 Prompt 版本" className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="space-y-1">
        <h2 className="text-base font-semibold">业务 Prompt 版本</h2>
        <p className="text-xs text-muted-foreground">集中查看 Agent 的 Prompt、上下文、历史发布与 Replay 指标。这里展示业务 Agent 的内容。</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {onManage && <Button size="sm" variant="outline" onClick={onManage}>登记与进化设置</Button>}
        <Button size="sm" variant="outline" disabled={loading} onClick={() => setRefreshKey(value => value + 1)}>
          {loading ? "加载中…" : "刷新版本"}
        </Button>
      </div>
    </div>
    <p className="rounded-lg border border-border bg-surface-subtle p-3 text-xs text-muted-foreground">
      TE 发布成功后，由 Agent 决定是否采用。下载、激活回执是 Agent 自报；确认模型实际使用仍需查看模型请求证据。
    </p>
    <label className="block max-w-md text-xs font-medium">查找执行配置或 Agent
      <input className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-normal"
        value={query} onChange={event => setQuery(event.target.value)} placeholder="输入配置名称、Agent 或用户" />
    </label>
    {error && <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/30 p-3 text-xs text-destructive">
      <span>{error}</span><Button size="sm" variant="outline" onClick={() => setRefreshKey(value => value + 1)}>重试加载</Button>
    </div>}
    {linkError && <p role="alert" className="rounded-md bg-muted p-3 text-xs text-muted-foreground">{linkError}</p>}
    {loading && !profiles.length && <p role="status" className="py-6 text-sm text-muted-foreground">正在读取已登记的业务 Prompt…</p>}
    {!loading && !error && !profiles.length && <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
      <p>尚未登记业务 Prompt。</p>
      <p className="mt-2 text-xs">先登记 Agent 当前使用的 Prompt 与上下文，之后可在这里查看版本和进化结果。</p>
      {onManage && <Button className="mt-3" size="sm" onClick={onManage}>登记业务 Prompt</Button>}
    </div>}
    {!loading && !!profiles.length && !filtered.length && <p className="py-6 text-sm text-muted-foreground">没有匹配的执行配置。</p>}
    <div className="space-y-3">
      {filtered.map(profile => <article key={profile.profile_id} className="min-w-0 rounded-xl border border-border bg-background p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 className="break-all text-sm font-semibold">{profile.profile_id}</h3>
            <p className="break-all text-xs text-muted-foreground">Agent：{profile.agent_id} · 用户：{profile.user_id}</p>
          </div>
          <Button size="sm" variant="outline" aria-label={`查看版本 · ${profile.profile_id}`} onClick={() => {
            setInitialReleaseId(""); setSelected(profile); setLinkError(""); updateLink(profile.profile_id);
          }}>查看版本与指标</Button>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {executionObjects(profile.execution_plan || { components: [] }).map(object => <span key={object.id}
            className="max-w-full break-all rounded-md bg-muted px-2 py-1 text-xs">
            {object.variableName ? `变量组 ${object.variableName} · ${object.valueCount} 个取值`
              : `${object.members[0].kind === "agent_prompt" ? "Prompt" : "上下文"} · ${object.id} · v${object.members[0].revision}`}
          </span>)}
          {!profile.execution_plan?.components?.length && <span className="text-xs text-muted-foreground">未记录业务组件</span>}
        </div>
        <div className="mt-3 break-all text-xs text-muted-foreground">TE 当前发布：{profile.current_release_id || "未发布"}</div>
        <div className="mt-1 text-xs text-muted-foreground">关联 Skill：{profile.affected_skill_ids?.length
          ? profile.affected_skill_ids.join("、") : "无（纯 Prompt 进化）"}</div>
      </article>)}
    </div>
    {selected && <ExecutionVersionDialog key={selected.profile_id} profile={selected} initialReleaseId={initialReleaseId}
      onReleaseChange={releaseId => updateLink(selected.profile_id, releaseId)}
      onClose={() => { setSelected(null); updateLink(); }} onManage={onManage} />}
  </section>;
}
