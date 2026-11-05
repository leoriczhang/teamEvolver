import { api } from "@/api/client";
import {
Empty,
ListViewport,
PaginationControls,
Panel,
Pill,
StatCard,
usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type EvolveHistoryCycle } from "@/features/skills/api/client";
import {
auditAction,auditCycle,auditReason,auditState,needsAuditRefresh,type AuditCycle,type AuditJob,
} from "@/features/skills/evolutionAudit";
import { fmtTime } from "@/lib/format";
import { toastErr } from "@/lib/toast";
import { useCallback,useEffect,useRef,useState } from "react";

export default function AuditView({ active }: { active: boolean }) {
  const [cycles, setCycles] = useState<AuditCycle[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [limit, setLimit] = useState(50);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState({ sessionId: "", limit: 50 });
  const requestSequence = useRef(0);
  const fetching = useRef(false);

  const refresh = useCallback(async (filters: typeof query, quiet = false) => {
    if (quiet && fetching.current) return;
    const sequence = ++requestSequence.current;
    fetching.current = true;
    if (!quiet) setLoading(true);
    try {
      const qs = new URLSearchParams();
      qs.set("limit", String(filters.limit));
      if (filters.sessionId) qs.set("session_id", filters.sessionId);
      const data = await api<{ cycles: EvolveHistoryCycle[] }>(`/history?${qs.toString()}`);
      const next = (data.cycles || []).map(auditCycle).filter((c): c is AuditCycle => c !== null);
      const unfinished = next.filter(c => !c.hasResult && c.command_id);
      // Unfinished history rows have no task status. Read it with bounded concurrency.
      for (let offset = 0; offset < unfinished.length; offset += 4) {
        if (sequence !== requestSequence.current) return;
        const batch = unfinished.slice(offset, offset + 4);
        const jobs = await Promise.allSettled(batch.map(c => api<AuditJob>(
          `/api/jobs/${encodeURIComponent(c.command_id!)}`,
        )));
        jobs.forEach((job, index) => {
          if (job.status === "fulfilled") {
            batch[index].taskStatus = job.value.status;
            batch[index].taskError = job.value.error_code;
          }
        });
      }
      if (sequence === requestSequence.current) setCycles(next);
    } catch (e: any) {
      if (sequence === requestSequence.current && !quiet) toastErr("加载审计记录失败", e.message);
    } finally {
      if (sequence === requestSequence.current) {
        fetching.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (active) void refresh(query);
    return () => { requestSequence.current++; };
  }, [active, query, refresh]);

  const pending = cycles.some(needsAuditRefresh);
  useEffect(() => {
    if (!active || !pending) return;
    const timer = window.setInterval(() => { void refresh(query, true); }, 5000);
    return () => window.clearInterval(timer);
  }, [active, pending, query, refresh]);

  function search() {
    const next = { sessionId: sessionId.trim(), limit };
    if (next.sessionId === query.sessionId && next.limit === query.limit) void refresh(query);
    else setQuery(next);
  }

  const uploaded = cycles.reduce((n, c) => n + Number(c.uploaded_skills || 0), 0);
  const queued = cycles.reduce((n, c) => n + Number(c.candidates_queued || 0), 0);
  const sessions = cycles.reduce((n, c) => n + Number(c.sessions ?? c.session_ids?.length ?? 0), 0);
  const cyclePager = usePagedItems(cycles);

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      <div className="mb-5 flex flex-wrap items-center justify-end gap-2">
        <Input
            value={sessionId}
            placeholder="按 session_id 过滤"
            className="h-8 w-[220px]"
            onChange={(e) => setSessionId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") search();
            }}
        />
        <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
          >
            {[20, 50, 100, 200].map((n) => (
              <option key={n} value={n}>最近 {n} 条记录</option>
            ))}
        </select>
        <Button variant="outline" size="sm" onClick={search} disabled={loading}>查询</Button>
      </div>
      <p className="mb-4 text-xs text-muted-foreground">
        仅展示 Skill 进化周期，目录检查和模型测试等辅助任务不计入。未完成任务每 5 秒自动刷新。
      </p>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="周期数" value={cycles.length} />
        <StatCard label="涉及会话" value={sessions} />
        <StatCard label="上传技能" value={uploaded} />
        <StatCard label="排队候选" value={queued} />
      </div>

      <Panel title="审计时间线" count={`${cycles.length} 个周期`}>
        {!cycles.length ? (
          <Empty>暂无进化历史，或当前过滤条件没有命中记录。</Empty>
        ) : (
          <>
            <ListViewport maxHeight="640px">
              <div className="divide-y divide-line">
                {cyclePager.items.map((c, i) => {
                  const evos = c.evolutions || [];
                  const judge = c.judge || {};
                  const sessionIds = c.session_ids || [];
                  const state = auditState(c);
                  return (
                    <div key={c.command_id || `${c.timestamp || "cycle"}-${cyclePager.start + i}`} className="p-4">
                      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-bold">{fmtTime(c.timestamp)}</div>
                          {c.command_id && <div className="mono mt-1 break-all text-[11px] text-muted-foreground">任务 ID：{c.command_id}</div>}
                          {c.finished_at && <div className="mt-1 text-xs text-muted-foreground">完成时间：{fmtTime(c.finished_at)}</div>}
                          <div className="mt-1 text-xs text-muted-foreground">
                            {c.hasResult
                              ? `${Number(c.sessions ?? sessionIds.length)} 会话 / ${c.skill_groups} 技能组 / 上传 ${c.uploaded_skills ?? 0} / 候选 ${c.candidates_queued ?? 0}`
                              : "结果尚未生成，处理数量待确认。"}
                          </div>
                        </div>
                        <Pill tone={state.tone}>{state.label}</Pill>
                      </div>
                      {c.taskError && <div className="mb-3 break-all text-xs text-destructive">任务错误：{c.taskError}</div>}

                      {judge.overall_score != null || judge.rationale ? (
                        <div className="mb-3 rounded-lg border border-border bg-background/60 p-3 text-sm">
                          <div className="mb-1 text-xs font-semibold text-muted-foreground">会话评审</div>
                          {judge.rationale ? (
                            <span className="text-muted-foreground">{judge.rationale}</span>
                          ) : (
                            <span className="text-muted-foreground">已完成会话评审</span>
                          )}
                        </div>
                      ) : null}

                      {sessionIds.length ? (
                        <details className="mb-3" open={!c.command_id}>
                          <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">消费会话（{sessionIds.length}）</summary>
                          <div className="mt-2 flex flex-wrap gap-1.5">
                            {sessionIds.map((sid) => (
                              <span key={sid} className="mono break-all rounded-md border border-border bg-surface-subtle px-2 py-1 text-[11px]">
                                {sid}
                              </span>
                            ))}
                          </div>
                        </details>
                      ) : null}

                      <div>
                        <div className="mb-1.5 text-xs font-semibold text-muted-foreground">分组结果</div>
                        {!evos.length ? (
                          <div className="text-xs text-muted-foreground">
                            {c.hasResult ? "本周期未生成候选，未记录分组原因。" : "任务尚未返回分组结果。"}
                          </div>
                        ) : (
                          <div className="space-y-2.5">
                            {evos.map((e, k) => {
                              const reason = e.reason || e.rationale || "";
                              return (
                                <div key={k} className="rounded-lg border border-line bg-surface-subtle p-3">
                                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="mono text-xs font-semibold">{e.skill_name === "__no_skill__" ? "未关联 Skill 的会话组" : e.skill_name || "-"}</span>
                                      <Pill tone={e.action === "skip" ? "gray" : "blue"}>{auditAction(e.action)}</Pill>
                                      {e.uploaded && <Pill tone="green">已上传</Pill>}
                                    </div>
                                    {e.version != null && <span className="text-xs text-muted-foreground">v{e.version}</span>}
                                  </div>
                                  {reason ? (
                                    <div className="mt-2 text-xs leading-relaxed text-muted-foreground">{auditReason(reason)}</div>
                                  ) : null}
                                  {(e.candidate_id || e.job_id) && <div className="mono mt-2 break-all text-[11px] text-muted-foreground">候选 ID：{e.candidate_id || e.job_id}</div>}
                                  {!!e.session_ids?.length && <div className="mt-2 text-[11px] text-muted-foreground">关联 {e.session_ids.length} 条 Session</div>}
                                  {e.file_changes?.length ? (
                                    <div className="mt-2 flex flex-wrap gap-1.5">
                                      {e.file_changes.map((change, index) => (
                                        <Pill key={`${change.path}-${index}`} tone={change.operation === "delete" ? "red" : "blue"}>
                                          {change.operation || "change"} · {change.path || "-"}
                                        </Pill>
                                      ))}
                                    </div>
                                  ) : null}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </ListViewport>
            <PaginationControls {...cyclePager} onPageChange={cyclePager.setPage} />
          </>
        )}
      </Panel>
    </div>
  );
}
