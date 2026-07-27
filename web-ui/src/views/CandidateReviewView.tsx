import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Panel,
  StatCard,
  Pill,
  ScoreText,
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

function replayIssue(ev?: EvalResult): string {
  if (!ev) return "";
  if (ev.replay?.error) return ev.replay.error;
  for (const item of ev.replay?.cases || []) {
    const err = item.baseline?.error || item.candidate?.error;
    if (err) return err;
  }
  return "";
}

function fmtScore(v?: number | null): string {
  return typeof v === "number" ? v.toFixed(3) : "—";
}

export default function CandidateReviewView({ active }: { active: boolean }) {
  const [cands, setCands] = useState<Candidate[]>([]);
  const [evalCache, setEvalCache] = useState<Record<string, EvalResult>>({});
  const [evaluating, setEvaluating] = useState<Record<string, boolean>>({});
  const [openJobId, setOpenJobId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const loaded = useRef(false);
  const evaluatingRef = useRef<Record<string, boolean>>({});
  const evalCacheRef = useRef<Record<string, EvalResult>>({});
  evaluatingRef.current = evaluating;
  evalCacheRef.current = evalCache;

  const evaluate = useCallback(async (jobId: string, force: boolean) => {
    if (evaluatingRef.current[jobId]) return;
    setEvaluating((m) => ({ ...m, [jobId]: true }));
    if (force) toastOk("已开始重新评估", "真实回放需拉起 Hermes A/B 分支，可能耗时数分钟，请勿离开或重复点击");
    try {
      const r = await api<EvalResult & { status?: string }>(
        `/validation/candidates/${encodeURIComponent(jobId)}/evaluate${force ? "?refresh=true" : ""}`,
        { method: "POST" }
      );
      if (r && r.status !== "not_found") {
        setEvalCache((m) => ({ ...m, [jobId]: r }));
        if (force) {
          const rep = r.replay || {};
          const issue = replayIssue(r);
          toastOk(
            "重新评估完成",
            issue
              ? `Verify ${fmtScore(r.verify_score)} / Replay ${fmtScore(r.replay_score)}｜${issue}`
              : `Verify ${fmtScore(r.verify_score)} / Replay ${fmtScore(r.replay_score)} / 基线 ${fmtScore(rep.baseline_mean)}`
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
      const data = await api<{ candidates: Candidate[] }>("/validation/candidates");
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
      for (const c of next) {
        if (!mergedCache[c.job_id] && !evaluatingRef.current[c.job_id]) {
          evaluate(c.job_id, false);
        }
      }
    } catch (e: any) {
      toastErr("加载候选失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [evaluate]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  async function validate(jobId: string, mode: "auto" | "force") {
    const msg =
      mode === "force"
        ? "确认强制发布该候选技能？此操作会绕过评分门槛。"
        : "确认按验证结果发布该候选技能？仅在回放无回退时发布。";
    if (!window.confirm(msg)) return;
    try {
      const r = await api<{ status?: string; version?: number }>(
        `/validation/candidates/${encodeURIComponent(jobId)}/validate`,
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
      await api(`/validation/candidates/${encodeURIComponent(jobId)}`, { method: "DELETE" });
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

  const evaluated = cands.filter((c) => evalCache[c.job_id]).length;
  const recommended = cands.filter((c) => evalCache[c.job_id]?.recommended_publish).length;
  const risky = cands.filter((c) => evalCache[c.job_id] && !evalCache[c.job_id].recommended_publish).length;
  const openCand = openJobId ? cands.find((c) => c.job_id === openJobId) || null : null;
  const candPager = usePagedItems(cands);

  return (
    <div className="mx-auto max-w-[1200px] px-7 py-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-bold tracking-tight">候选评审</h1>
          <div className="mt-1 text-xs text-muted-foreground">
            集中处理待发布技能候选，查看 Verify 与 True Replay 证据后再发布。
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          刷新
        </Button>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="待评审候选" value={cands.length} />
        <StatCard label="已完成评估" value={evaluated} />
        <StatCard label="建议发布" value={recommended} />
        <StatCard label="建议复核" value={risky} />
      </div>

      <Panel title="评审队列" count={`${cands.length} 个`}>
        {!cands.length ? (
          <Empty>暂无待发布候选。</Empty>
        ) : (
          <>
            <ListViewport>
              <Table
                headers={[
                  "技能",
                  "动作",
                  "Verify",
                  "True Replay",
                  "基线",
                  "建议",
                  "操作",
                ]}
              >
                {candPager.items.map((c) => {
                  const ev = evalCache[c.job_id] || c.evaluation;
                  const busy = !!evaluating[c.job_id];
                  const rep = ev?.replay || {};
                  const issue = replayIssue(ev);
                  return (
                    <tr key={c.job_id}>
                      <td className="link border-b border-line px-4 py-2.5 align-top" onClick={() => setOpenJobId(c.job_id)}>
                        <div className="font-semibold">{candidateName(c)}</div>
                        <div className="mt-1 max-w-[320px] truncate text-xs text-muted-foreground">{c.rationale || c.job_id}</div>
                      </td>
                      <Td><Pill tone="blue">{c.proposed_action || "-"}</Pill></Td>
                      <Td>{busy ? <span className="score pending">评估中…</span> : <ScoreText value={ev?.verify_score} threshold={ev?.verification?.threshold} pending="待评估" />}</Td>
                      <Td>
                        {busy ? <span className="score pending">评估中…</span> : <ScoreText value={ev?.replay_score} threshold={rep.threshold ?? c.min_score ?? 0.75} pending="待评估" />}
                        {!busy && issue && <div className="mt-1 max-w-[220px] truncate text-[11px] text-destructive" title={issue}>{issue}</div>}
                      </Td>
                      <Td>{busy ? <span className="score pending">—</span> : <ScoreText value={rep.baseline_mean} />}</Td>
                      <Td>
                        {ev ? (
                          ev.recommended_publish ? <Pill tone="green">建议发布</Pill> : <Pill tone="red">建议复核</Pill>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1.5">
                          <Button variant="outline" size="sm" disabled={busy} onClick={() => evaluate(c.job_id, true)}>
                            {busy ? "评估中…" : "重新评估"}
                          </Button>
                          <Button size="sm" onClick={() => validate(c.job_id, "auto")}>验证发布</Button>
                          <Button variant="outline" size="sm" onClick={() => validate(c.job_id, "force")}>强制发布</Button>
                          <Button variant="destructive" size="sm" onClick={() => deleteCandidate(c.job_id)}>删除</Button>
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
        ev={openJobId ? evalCache[openJobId] ?? null : null}
        evaluating={openJobId ? !!evaluating[openJobId] : false}
        open={!!openJobId}
        onClose={() => setOpenJobId(null)}
        onEvaluate={(force) => openJobId && evaluate(openJobId, force)}
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
