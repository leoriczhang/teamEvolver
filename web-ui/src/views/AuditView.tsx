import { useCallback, useEffect, useRef, useState } from "react";
import {
  Panel,
  StatCard,
  Pill,
  Empty,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, type EvolveHistoryCycle } from "@/api/client";
import { fmtTime } from "@/lib/format";
import { toastErr } from "@/lib/toast";

export default function AuditView({ active }: { active: boolean }) {
  const [cycles, setCycles] = useState<EvolveHistoryCycle[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [limit, setLimit] = useState(50);
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams();
      qs.set("limit", String(limit || 50));
      if (sessionId.trim()) qs.set("session_id", sessionId.trim());
      const data = await api<{ cycles: EvolveHistoryCycle[] }>(`/history?${qs.toString()}`);
      setCycles(data.cycles || []);
    } catch (e: any) {
      toastErr("加载审计记录失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [limit, sessionId]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  const uploaded = cycles.reduce((n, c) => n + Number(c.uploaded_skills || 0), 0);
  const queued = cycles.reduce((n, c) => n + Number(c.candidates_queued || 0), 0);
  const sessions = cycles.reduce((n, c) => n + Number(c.sessions || (c.session_ids || []).length || 0), 0);
  const cyclePager = usePagedItems(cycles);

  return (
    <div className="mx-auto max-w-[1200px] px-7 py-6">
      <div className="mb-5 flex flex-wrap items-center justify-end gap-2">
        <Input
            value={sessionId}
            placeholder="按 session_id 过滤"
            className="h-8 w-[220px]"
            onChange={(e) => setSessionId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") refresh();
            }}
        />
        <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
          >
            {[20, 50, 100, 200].map((n) => (
              <option key={n} value={n}>最近 {n} 条</option>
            ))}
        </select>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>查询</Button>
      </div>

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
                  const noActionNormal = isNoActionNormal(c, evos);
                  return (
                    <div key={`${c.timestamp || "cycle"}-${cyclePager.start + i}`} className="p-4">
                      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-bold">{fmtTime(c.timestamp)}</div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {Number(c.sessions || sessionIds.length || 0)} 会话 / {c.skill_groups ?? "?"} 技能组 / 上传 {c.uploaded_skills ?? 0} / 候选 {c.candidates_queued ?? 0}
                          </div>
                        </div>
                        <Pill tone={Number(c.uploaded_skills || 0) > 0 ? "green" : Number(c.candidates_queued || 0) > 0 ? "amber" : noActionNormal ? "blue" : "gray"}>
                          {Number(c.uploaded_skills || 0) > 0 ? "已产出" : Number(c.candidates_queued || 0) > 0 ? "待评审" : noActionNormal ? "无需进化" : "无变更"}
                        </Pill>
                      </div>

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
                        <div className="mb-3">
                          <div className="mb-1.5 text-xs font-semibold text-muted-foreground">消费会话</div>
                          <div className="flex flex-wrap gap-1.5">
                            {sessionIds.slice(0, 12).map((sid) => (
                              <span key={sid} className="mono rounded-md border border-border bg-surface-subtle px-2 py-1 text-[11px]">
                                {sid}
                              </span>
                            ))}
                            {sessionIds.length > 12 && <span className="text-xs text-muted-foreground">等 {sessionIds.length} 个</span>}
                          </div>
                        </div>
                      ) : null}

                      <div>
                        <div className="mb-1.5 text-xs font-semibold text-muted-foreground">技能变更</div>
                        {!evos.length ? (
                          noActionNormal ? (
                            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
                              <Pill tone="blue">流程正常</Pill>
                              <div className="mt-2">
                                本周期已完成会话评审和 Skill 分组，但 planner 判断当前 Skill 无需优化，也无需创建新 Skill。
                              </div>
                            </div>
                          ) : (
                            <div className="text-xs text-muted-foreground">本周期未记录技能变更。</div>
                          )
                        ) : (
                          <div className="space-y-2.5">
                            {evos.map((e, k) => {
                              const legacyVerifier = e.action === "verification_rejected";
                              const reason = e.reason || e.rationale || "";
                              return (
                                <div key={k} className="rounded-lg border border-line bg-surface-subtle p-3">
                                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="mono text-xs font-semibold">{e.skill_name || "-"}</span>
                                      <Pill tone={legacyVerifier ? "gray" : "blue"}>
                                        {legacyVerifier ? "未进入回放（旧流程）" : e.action || "-"}
                                      </Pill>
                                      {e.uploaded ? <Pill tone="green">已上传</Pill> : <Pill tone="gray">未上传</Pill>}
                                    </div>
                                    {e.version != null && <span className="text-xs text-muted-foreground">v{e.version}</span>}
                                  </div>
                                  {legacyVerifier ? (
                                    <div className="mt-2 text-xs leading-relaxed text-muted-foreground">
                                      该记录由旧版预检流程产生；当前版本仅比较 True Replay 的轮次、工具调用和 Token。
                                    </div>
                                  ) : reason ? (
                                    <div className="mt-2 text-xs leading-relaxed text-muted-foreground">{reason}</div>
                                  ) : null}
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

function isNoActionNormal(c: EvolveHistoryCycle, evos: unknown[]) {
  return (
    !evos.length &&
    !c.had_processing_error &&
    Number(c.sessions || (c.session_ids || []).length || 0) > 0 &&
    Number(c.skill_groups || 0) > 0 &&
    Number(c.actions || 0) === 0 &&
    Number(c.uploaded_skills || 0) === 0 &&
    Number(c.candidates_queued || 0) === 0
  );
}
