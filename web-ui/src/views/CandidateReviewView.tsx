import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
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
import { api, type Candidate, type EvalResult } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import CandidateModal from "./dashboard/CandidateModal";

function candidateName(c: Candidate): string {
  return c.skill_name || c.candidate_skill_name || c.candidate_skill?.name || c.job_id;
}

function mergeCandidateDetail(items: Candidate[], detail: Candidate): Candidate[] {
  const found = items.some((item) => item.job_id === detail.job_id);
  if (!found) return [detail, ...items];
  return items.map((item) => (
    item.job_id === detail.job_id ? { ...item, ...detail } : item
  ));
}

function replayIssue(ev?: EvalResult): string {
  if (!ev) return "";
  if (ev.replay?.error) return ev.replay.error;
  for (const item of ev.replay?.cases || []) {
    const err = item.baseline?.error || item.candidate?.error;
    if (err) return err;
  }
  return "";
}

type CandidateScope = "open" | "processed";

function candidateReviewStatus(c: Candidate): string {
  return c.review_status || c.decision?.status || "open";
}

function isProcessedCandidate(c: Candidate | null): boolean {
  if (!c) return false;
  return candidateReviewStatus(c) !== "open" || !!c.decision;
}

function candidateStatusBadge(c: Candidate): ReactNode {
  const status = candidateReviewStatus(c);
  if (status === "published") return <Pill tone="green">已发布</Pill>;
  if (status === "rejected") return <Pill tone="red">已拒绝</Pill>;
  if (status === "deleted") return <Pill tone="gray">已删除</Pill>;
  if (status === "open") return <Pill tone="amber">待处理</Pill>;
  return <Pill tone="gray">{status || "未知"}</Pill>;
}

function shortTime(value?: string): string {
  if (!value) return "";
  return value.slice(0, 19).replace("T", " ");
}

export default function CandidateReviewView({ active }: { active: boolean }) {
  const [cands, setCands] = useState<Candidate[]>([]);
  const [evalCache, setEvalCache] = useState<Record<string, EvalResult>>({});
  const [evaluating, setEvaluating] = useState<Record<string, boolean>>({});
  const [openJobId, setOpenJobId] = useState<string | null>(null);
  const [scope, setScope] = useState<CandidateScope>("open");
  const [loading, setLoading] = useState(false);
  const evaluatingRef = useRef<Record<string, boolean>>({});
  const evalCacheRef = useRef<Record<string, EvalResult>>({});
  evaluatingRef.current = evaluating;
  evalCacheRef.current = evalCache;

  const loadCandidateDetail = useCallback(async (jobId: string) => {
    try {
      const detail = await api<Candidate>(
        `/api/validation/candidates/${encodeURIComponent(jobId)}/detail`
      );
      setCands((items) => mergeCandidateDetail(items, detail));
      if (detail.evaluation) {
        setEvalCache((m) => ({ ...m, [jobId]: detail.evaluation as EvalResult }));
      }
      return detail;
    } catch (e: any) {
      toastErr("加载评估详情失败", e.message);
      return null;
    }
  }, []);

  const openCandidate = useCallback((jobId: string) => {
    setOpenJobId(jobId);
    void loadCandidateDetail(jobId);
  }, [loadCandidateDetail]);

  const evaluate = useCallback(async (jobId: string, force: boolean) => {
    if (evaluatingRef.current[jobId]) return;
    setEvaluating((m) => ({ ...m, [jobId]: true }));
    if (force) toastOk("已开始重新评估", "真实回放需拉起 Hermes A/B 分支，可能耗时数分钟，请勿离开或重复点击");
    try {
      const r = await api<EvalResult & { status?: string }>(
        `/api/validation/candidates/${encodeURIComponent(jobId)}/evaluate${force ? "?refresh=true" : ""}`,
        { method: "POST" }
      );
      if (r && r.status !== "not_found") {
        setEvalCache((m) => ({ ...m, [jobId]: r }));
        if (force) {
          const issue = replayIssue(r);
          toastOk(
            "重新评估完成",
            issue
              ? `回放存在问题：${issue}`
              : r.recommended_publish
                ? "客观指标改善"
                : "客观指标持平或增加"
          );
        }
      }
    } catch (e: any) {
      toastErr("评估失败", e.message);
    } finally {
      setEvaluating((m) => ({ ...m, [jobId]: false }));
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const query = scope === "processed" ? "?scope=processed" : "";
      const data = await api<{ candidates: Candidate[] }>(`/api/validation/candidates${query}`);
      const next = data.candidates || [];
      const serverEvaluations: Record<string, EvalResult> = {};
      for (const c of next) {
        if (c.evaluation) serverEvaluations[c.job_id] = c.evaluation;
      }
      const mergedCache = { ...evalCacheRef.current, ...serverEvaluations };
      if (Object.keys(serverEvaluations).length) {
        setEvalCache((m) => ({ ...m, ...serverEvaluations }));
      }
      setCands(next);
      if (scope === "open") {
        for (const c of next) {
          if (!mergedCache[c.job_id] && !evaluatingRef.current[c.job_id]) {
            evaluate(c.job_id, false);
          }
        }
      }
    } catch (e: any) {
      toastErr("加载候选失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [evaluate, scope]);

  useEffect(() => {
    if (active) {
      refresh();
    }
  }, [active, refresh]);

  async function validate(jobId: string, mode: "auto" | "force") {
    const msg =
      mode === "force"
        ? "确认强制发布该候选技能？仍会保留 True Replay 三项指标。"
        : "确认按 True Replay 发布？轮次下降直接正向；轮次持平时再比较工具调用和 Token。";
    if (!window.confirm(msg)) return;
    try {
      const r = await api<{ status?: string; version?: number }>(
        `/api/validation/candidates/${encodeURIComponent(jobId)}/validate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }
      );
      toastOk("发布结果", (r.status || "done") + (r.version ? ` (v${r.version})` : ""));
      setOpenJobId((cur) => (cur === jobId ? null : cur));
      setEvalCache((m) => {
        const n = { ...m };
        delete n[jobId];
        return n;
      });
      await refresh();
    } catch (e: any) {
      toastErr("发布失败", e.message);
    }
  }

  async function deleteCandidate(jobId: string) {
    if (!window.confirm("确认删除该待发布候选？删除后将从评审队列移除。")) return;
    try {
      await api(`/api/validation/candidates/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      toastOk("已删除候选");
      setOpenJobId((cur) => (cur === jobId ? null : cur));
      setEvalCache((m) => {
        const n = { ...m };
        delete n[jobId];
        return n;
      });
      await refresh();
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  const isHistory = scope === "processed";
  const evaluated = cands.filter((c) => evalCache[c.job_id] || c.evaluation).length;
  const recommended = cands.filter((c) => (evalCache[c.job_id] || c.evaluation)?.recommended_publish).length;
  const risky = cands.filter((c) => {
    const ev = evalCache[c.job_id] || c.evaluation;
    return ev && !ev.recommended_publish;
  }).length;
  const published = cands.filter((c) => candidateReviewStatus(c) === "published").length;
  const rejected = cands.filter((c) => candidateReviewStatus(c) === "rejected").length;
  const openCand = openJobId ? cands.find((c) => c.job_id === openJobId) || null : null;
  const candPager = usePagedItems(cands);

  return (
    <div className="mx-auto max-w-[1200px] px-7 py-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-bold tracking-tight">候选评审</h1>
          <div className="mt-1 text-xs text-muted-foreground">
            集中处理待发布技能候选，也可回看已发布或已拒绝的历史候选。
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={scope === "open" ? "default" : "outline"}
            size="sm"
            onClick={() => setScope("open")}
          >
            待处理
          </Button>
          <Button
            variant={scope === "processed" ? "default" : "outline"}
            size="sm"
            onClick={() => setScope("processed")}
          >
            已处理/历史
          </Button>
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            刷新
          </Button>
        </div>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label={isHistory ? "历史候选" : "待评审候选"} value={cands.length} />
        <StatCard label={isHistory ? "已发布" : "已完成评估"} value={isHistory ? published : evaluated} />
        <StatCard label={isHistory ? "已拒绝" : "指标改善"} value={isHistory ? rejected : recommended} />
        <StatCard label={isHistory ? "带回放证据" : "持平或增加"} value={isHistory ? evaluated : risky} />
      </div>

      <Panel title={isHistory ? "已处理/历史候选" : "评审队列"} count={`${cands.length} 个`}>
        {!cands.length ? (
          <Empty>{isHistory ? "暂无已处理候选。" : "暂无待发布候选。"}</Empty>
        ) : (
          <>
            <ListViewport>
              <Table
                headers={[
                  "技能",
                  "动作",
                  "状态",
                  "回放状态",
                  "客观结论",
                  "操作",
                ]}
              >
                {candPager.items.map((c) => {
                  const ev = evalCache[c.job_id] || c.evaluation;
                  const busy = !!evaluating[c.job_id];
                  const processed = isProcessedCandidate(c);
                  const issue = replayIssue(ev);
                  const decisionReason = c.decision_reason || c.decision?.reason || "";
                  const decidedAt = shortTime(c.decided_at || c.decision?.decided_at);
                  return (
                    <tr key={c.job_id}>
                      <td className="link border-b border-line px-4 py-2.5 align-top" onClick={() => openCandidate(c.job_id)}>
                        <div className="font-semibold">{candidateName(c)}</div>
                        {(c.bundle_diff?.changed_count || 0) > 0 && (
                          <div className="mt-1">
                            <Pill tone="blue">变更 {c.bundle_diff?.changed_count} 个文件</Pill>
                          </div>
                        )}
                        <div className="mt-1 max-w-[320px] truncate text-xs text-muted-foreground" title={c.rationale || c.job_id}>
                          {c.rationale || c.job_id}
                        </div>
                      </td>
                      <Td><Pill tone="blue">{c.proposed_action || "-"}</Pill></Td>
                      <Td>
                        <div className="flex flex-col items-start gap-1">
                          {candidateStatusBadge(c)}
                          {decidedAt && <span className="text-[11px] text-muted-foreground">{decidedAt}</span>}
                          {decisionReason && (
                            <span className="max-w-[220px] truncate text-[11px] text-muted-foreground" title={decisionReason}>
                              {decisionReason}
                            </span>
                          )}
                        </div>
                      </Td>
                      <Td>
                        {busy ? <Pill tone="amber">评估中…</Pill> : ev ? <Pill tone="blue">已完成</Pill> : <Pill tone="gray">待评估</Pill>}
                        {(ev?.static_validation || c.static_validation) && (
                          <div className="mt-1">
                            <Pill tone={(ev?.static_validation || c.static_validation)?.passed ? "green" : "red"}>
                              静态检查{(ev?.static_validation || c.static_validation)?.passed ? "通过" : "失败"}
                            </Pill>
                          </div>
                        )}
                        {!busy && issue && <div className="mt-1 max-w-[220px] truncate text-[11px] text-destructive" title={issue}>{issue}</div>}
                      </Td>
                      <Td>
                        {ev ? (
                          ev.recommended_publish ? <Pill tone="green">指标改善</Pill> : <Pill tone="amber">持平 / 有增加</Pill>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1.5">
                          {processed ? (
                            <Button variant="outline" size="sm" onClick={() => openCandidate(c.job_id)}>
                              查看详情
                            </Button>
                          ) : (
                            <>
                              <Button variant="outline" size="sm" disabled={busy} onClick={() => evaluate(c.job_id, true)}>
                                {busy ? "评估中…" : "重新评估"}
                              </Button>
                              <Button size="sm" onClick={() => validate(c.job_id, "auto")}>按回放发布</Button>
                              <Button variant="outline" size="sm" onClick={() => validate(c.job_id, "force")}>强制发布</Button>
                              <Button variant="destructive" size="sm" onClick={() => deleteCandidate(c.job_id)}>删除</Button>
                            </>
                          )}
                        </div>
                      </Td>
                    </tr>
                  );
                })}
              </Table>
            </ListViewport>
            <PaginationControls {...candPager} onPageChange={candPager.setPage} />
          </>
        )}
      </Panel>

      <CandidateModal
        jobId={openJobId}
        cand={openCand}
        ev={openJobId ? evalCache[openJobId] ?? openCand?.evaluation ?? null : null}
        evaluating={openJobId ? !!evaluating[openJobId] : false}
        open={!!openJobId}
        readOnly={isProcessedCandidate(openCand)}
        onClose={() => setOpenJobId(null)}
        onEvaluate={(force) => {
          if (!openJobId || isProcessedCandidate(openCand)) return;
          evaluate(openJobId, force);
        }}
      />
    </div>
  );
}

function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return (
    <table className="w-full border-collapse">
      <thead>
        <tr>
          {headers.map((h) => (
            <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

function Td({ children }: { children: ReactNode }) {
  return <td className="border-b border-line px-4 py-2.5 align-top">{children}</td>;
}
