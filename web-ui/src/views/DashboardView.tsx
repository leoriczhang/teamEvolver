import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Panel,
  StatCard,
  UserBadge,
  Pill,
  Dot,
  ScoreText,
  Empty,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { fmtTime } from "@/lib/format";
import { toastOk, toastErr } from "@/lib/toast";
import {
  api,
  type StatusResp,
  type StorageStatus,
  type QueueSession,
  type LedgerRow,
  type Candidate,
  type EvalResult,
  type PageResponse,
} from "@/api/client";
import SkillVersionModal from "./dashboard/SkillVersionModal";
import SessionModal, { StatusBadge, type SessTab } from "./dashboard/SessionModal";
import CandidateModal from "./dashboard/CandidateModal";

const POLL_MS = 15_000;
const PAGE_SIZE = 20;

function mergeCandidateDetail(items: Candidate[], detail: Candidate): Candidate[] {
  const found = items.some((item) => item.job_id === detail.job_id);
  if (!found) return [detail, ...items];
  return items.map((item) => (
    item.job_id === detail.job_id ? { ...item, ...detail } : item
  ));
}

function serverPager(page: number, total: number, itemCount: number) {
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(Math.max(1, page), totalPages);
  const start = (currentPage - 1) * PAGE_SIZE;
  return {
    page: currentPage,
    totalPages,
    visiblePages: Math.min(10, totalPages),
    total,
    start,
    end: start + itemCount,
  };
}

export default function DashboardView({ active }: { active: boolean }) {
  const [status, setStatus] = useState<StatusResp | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [queue, setQueue] = useState<QueueSession[] | null>(null);
  const [ledger, setLedger] = useState<LedgerRow[] | null>(null);
  const [cands, setCands] = useState<Candidate[]>([]);
  const [queueTotal, setQueueTotal] = useState(0);
  const [ledgerTotal, setLedgerTotal] = useState(0);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [queuePage, setQueuePage] = useState(1);
  const [ledgerPage, setLedgerPage] = useState(1);
  const [candidatePage, setCandidatePage] = useState(1);
  const [lastUpdate, setLastUpdate] = useState("—");

  const [evalCache, setEvalCache] = useState<Record<string, EvalResult>>({});
  const [evaluating, setEvaluating] = useState<Record<string, boolean>>({});

  const inflight = useRef(false);
  const evaluatingRef = useRef<Record<string, boolean>>({});
  const evalCacheRef = useRef<Record<string, EvalResult>>({});
  evaluatingRef.current = evaluating;
  evalCacheRef.current = evalCache;

  // modal state
  const [skillModal, setSkillModal] = useState<{ name: string; version: number } | null>(null);
  const [sessModal, setSessModal] = useState<{ sid: string; tab: SessTab } | null>(null);
  const [candJobId, setCandJobId] = useState<string | null>(null);

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
    setCandJobId(jobId);
    void loadCandidateDetail(jobId);
  }, [loadCandidateDetail]);

  const evaluate = useCallback(async (jobId: string, force: boolean) => {
    if (evaluatingRef.current[jobId]) return;
    setEvaluating((m) => ({ ...m, [jobId]: true }));
    try {
      const r = await api<EvalResult & { status?: string }>(
        `/api/validation/candidates/${encodeURIComponent(jobId)}/evaluate${force ? "?refresh=true" : ""}`,
        { method: "POST" }
      );
      if (r && r.status !== "not_found") {
        setEvalCache((m) => ({ ...m, [jobId]: r }));
      }
    } catch (e: any) {
      console.warn("evaluate failed", jobId, e.message);
    } finally {
      setEvaluating((m) => ({ ...m, [jobId]: false }));
    }
  }, []);

  const refresh = useCallback(
    async (force: boolean) => {
      if (inflight.current && !force) return;
      inflight.current = true;
      const refreshFlag = force ? "&refresh=true" : "";
      const [statusResult, storageResult, queueResult, ledgerResult, candidateResult] =
        await Promise.allSettled([
          api<StatusResp>(`/status${force ? "?refresh=true" : ""}`),
          api<StorageStatus>("/storage/status"),
          api<PageResponse<QueueSession>>(
            `/sessions?limit=${PAGE_SIZE}&offset=${(queuePage - 1) * PAGE_SIZE}${refreshFlag}`
          ),
          api<PageResponse<LedgerRow>>(
            `/conversations?limit=${PAGE_SIZE}&offset=${(ledgerPage - 1) * PAGE_SIZE}${refreshFlag}`
          ),
          api<PageResponse<Candidate>>(
            `/api/validation/candidates?compact=true&limit=${PAGE_SIZE}&offset=${(candidatePage - 1) * PAGE_SIZE}${refreshFlag}`
          ),
        ]);
      const st = statusResult.status === "fulfilled" ? statusResult.value : null;
      const sto = storageResult.status === "fulfilled" ? storageResult.value : null;
      const queuePayload = queueResult.status === "fulfilled" ? queueResult.value : null;
      const ledgerPayload = ledgerResult.status === "fulfilled" ? ledgerResult.value : null;
      const candidatePayload =
        candidateResult.status === "fulfilled" ? candidateResult.value : null;
      const q = queuePayload?.sessions || null;
      const led = ledgerPayload?.conversations || null;
      const cs = candidatePayload?.candidates || null;
      const nextCands = cs || [];
      const serverEvaluations: Record<string, EvalResult> = {};
      for (const c of nextCands) {
        if (c.evaluation) serverEvaluations[c.job_id] = c.evaluation;
      }
      const mergedCache = { ...evalCacheRef.current, ...serverEvaluations };
      if (Object.keys(serverEvaluations).length) {
        setEvalCache((m) => ({ ...m, ...serverEvaluations }));
      }
      setStatus(st);
      setStorage(sto);
      setQueue(q);
      setLedger(led);
      setCands(nextCands);
      setQueueTotal(queuePayload?.total || 0);
      setLedgerTotal(ledgerPayload?.total || 0);
      setCandidateTotal(candidatePayload?.total || 0);
      setLastUpdate(
        "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false })
      );
      inflight.current = false;
      // auto-evaluate un-cached candidates
      for (const c of nextCands) {
        if (!mergedCache[c.job_id] && !evaluatingRef.current[c.job_id]) {
          evaluate(c.job_id, false);
        }
      }
    },
    [candidatePage, evaluate, ledgerPage, queuePage]
  );

  // poll
  useEffect(() => {
    if (!active) return;
    refresh(true);
    const id = setInterval(() => refresh(false), POLL_MS);
    return () => clearInterval(id);
  }, [active, refresh]);

  // ---- actions ---- //
  async function validate(jobId: string, mode: "auto" | "force") {
    const msg =
      mode === "force"
        ? "确认强制发布该候选技能？（将忽略评分直接发布）"
        : "确认按验证结果发布该候选技能？（回放无回退才会发布）";
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
      setEvalCache((m) => {
        const n = { ...m };
        delete n[jobId];
        return n;
      });
      if (candJobId === jobId) setCandJobId(null);
      refresh(true);
    } catch (e: any) {
      toastErr("发布失败", e.message);
    }
  }

  async function deleteCandidate(jobId: string) {
    if (!window.confirm("确认删除该待发布候选？删除后将从队列移除且不可恢复。")) return;
    try {
      await api(`/api/validation/candidates/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      setEvalCache((m) => {
        const n = { ...m };
        delete n[jobId];
        return n;
      });
      if (candJobId === jobId) setCandJobId(null);
      toastOk("已删除候选");
      refresh(true);
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  const running = status?.running;
  const skills = status?.skills || {};
  const skillNames = Object.keys(skills);
  const openCand = candJobId ? cands.find((c) => c.job_id === candJobId) || null : null;
  const skillPager = usePagedItems(skillNames);
  const queuePager = serverPager(queuePage, queueTotal, queue?.length || 0);
  const ledgerPager = serverPager(ledgerPage, ledgerTotal, ledger?.length || 0);
  const candPager = serverPager(candidatePage, candidateTotal, cands.length);

  return (
    <div className="mx-auto max-w-[1200px] px-7 py-6">
      {/* header */}
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2.5 text-[22px] font-bold tracking-tight">
            <Dot state={status ? (running ? "run" : "on") : "err"} /> 进化看板
          </h1>
          <div className="mt-1 text-xs text-muted-foreground">
            会话进化流水线 · 待发布候选 · 技能版本
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ConnBadge storage={storage} />
          <span className="text-xs text-muted-foreground">{lastUpdate}</span>
          <Button variant="outline" size="sm" onClick={() => refresh(true)}>
            刷新
          </Button>
        </div>
      </div>

      {/* stats */}
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="运行状态" value={status ? (running ? "进化中" : "空闲") : "不可达"} />
        <StatCard label="排队会话" value={status ? status.pending_sessions : "—"} />
        <StatCard label="已注册技能" value={status ? status.registered_skills : "—"} />
        <StatCard
          label="存储连接"
          value={storage ? (storage.reachable ? "正常" : "异常") : "—"}
        />
      </div>

      {/* queue */}
      <Panel title="会话队列" count={queue ? `${queueTotal} 个` : ""}>
        {!queue?.length ? (
          <Empty>队列为空，暂无待进化会话</Empty>
        ) : (
          <>
            <ListViewport>
              <Table headers={["提交人", "会话 ID", "轮数", "提交时间"]}>
                {(queue || []).map((s, i) => (
                  <tr key={`${s.session_id}-${i}`}>
                    <Td>
                      <UserBadge name={s.user_alias} />
                    </Td>
                    <Td className="mono">{s.session_id}</Td>
                    <Td>{s.num_turns}</Td>
                    <Td className="text-xs text-muted-foreground">{fmtTime(s.timestamp)}</Td>
                  </tr>
                ))}
              </Table>
            </ListViewport>
            <PaginationControls {...queuePager} onPageChange={setQueuePage} />
          </>
        )}
      </Panel>

      {/* history */}
      <Panel title="会话历史" count={ledger ? `${ledgerTotal} 条` : ""}>
        {!ledger?.length ? (
          <Empty>尚无会话历史</Empty>
        ) : (
          <>
            <ListViewport>
              <Table headers={["会话标题", "提交人", "轮数", "消费状态", "时间"]}>
                {(ledger || []).map((r, i) => {
                  const sid = r.session_id || "";
                  return (
                    <tr key={`${sid}-${i}`}>
                      <td
                        className="link max-w-[360px] truncate border-b border-line px-4 py-2.5 align-top text-[#2563eb]"
                        title={"点击查看会话内容：" + (r.title || "")}
                        onClick={() => setSessModal({ sid, tab: "detail" })}
                      >
                        {r.title || "(无标题会话)"}
                      </td>
                      <Td>
                        <UserBadge name={r.user_alias} />
                      </Td>
                      <Td>{r.num_turns != null ? r.num_turns : "-"}</Td>
                      <td
                        className="link border-b border-line px-4 py-2.5 align-top"
                        title="点击查看进化过程明细"
                        onClick={() => setSessModal({ sid, tab: "process" })}
                      >
                        <StatusBadge status={r.status} />
                      </td>
                      <Td className="text-xs text-muted-foreground">
                        {fmtTime(r.consumed_at || r.ingested_at || r.timestamp)}
                      </Td>
                    </tr>
                  );
                })}
              </Table>
            </ListViewport>
            <PaginationControls {...ledgerPager} onPageChange={setLedgerPage} />
          </>
        )}
      </Panel>

      {/* candidates */}
      <Panel title="待发布候选" count={candidateTotal ? `${candidateTotal} 个` : ""}>
        {!cands.length ? (
          <Empty>暂无待发布候选</Empty>
        ) : (
          <>
            <ListViewport>
              <Table
                headers={[
                  "技能",
                  "动作",
                  "验证分 (Verify)",
                  "A/B 回放分",
                  "基线",
                  "建议",
                  "操作",
                ]}
              >
                {cands.map((c) => {
              const ev = evalCache[c.job_id];
              const busy = evaluating[c.job_id];
              const thr = c.min_score != null ? c.min_score : 0.75;
              const open = () => openCandidate(c.job_id);
              const rep = ev?.replay || {};
              return (
                <tr key={c.job_id}>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {c.skill_name}
                  </td>
                  <Td>
                    <Pill tone="blue">{c.proposed_action || "-"}</Pill>
                  </Td>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {busy && !ev ? (
                      <span className="score pending">评估中…</span>
                    ) : ev ? (
                      <ScoreText value={ev.verify_score} threshold={ev.verification?.threshold} />
                    ) : (
                      <span className="score pending">待评估</span>
                    )}
                  </td>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {busy && !ev ? (
                      <span className="score pending">评估中…</span>
                    ) : ev ? (
                      <ScoreText
                        value={ev.replay_score}
                        threshold={rep.threshold != null ? rep.threshold : thr}
                      />
                    ) : (
                      <span className="score pending">待评估</span>
                    )}
                  </td>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {ev ? <ScoreText value={rep.baseline_mean} /> : <span className="score pending">—</span>}
                  </td>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {ev ? (
                      ev.recommended_publish ? (
                        <Pill tone="green">建议发布</Pill>
                      ) : (
                        <Pill tone="red">建议复核</Pill>
                      )
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="border-b border-line px-4 py-2.5 align-top">
                    <div className="flex flex-wrap gap-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => evaluate(c.job_id, true)}
                      >
                        重新评估
                      </Button>
                      <Button size="sm" onClick={() => validate(c.job_id, "auto")}>
                        验证发布
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => validate(c.job_id, "force")}>
                        强制发布
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => deleteCandidate(c.job_id)}
                      >
                        删除
                      </Button>
                    </div>
                  </td>
                </tr>
              );
                })}
              </Table>
            </ListViewport>
            <PaginationControls {...candPager} onPageChange={setCandidatePage} />
          </>
        )}
      </Panel>

      {/* skill versions */}
      <Panel
        title={
          <>
            技能版本{" "}
            <span className="text-xs font-normal text-muted-foreground">
              （点击行查看详情 / 切换版本）
            </span>
          </>
        }
        count={`${skillNames.length} 个`}
      >
        {!skillNames.length ? (
          <Empty>注册表为空</Empty>
        ) : (
          <>
            <ListViewport>
              <Table headers={["技能名", "Skill ID", "版本", "操作"]}>
                {skillPager.items.map((n) => {
                  const s = skills[n] || {};
                  const v = s.version || 0;
                  const canRoll = v > 1;
                  return (
                    <tr
                      key={n}
                      className="clickable"
                      onClick={() => setSkillModal({ name: n, version: v })}
                    >
                      <Td>{n}</Td>
                      <Td className="mono">{s.skill_id || "-"}</Td>
                      <Td>
                        <Pill tone="green">v{v}</Pill>
                      </Td>
                      <td className="border-b border-line px-4 py-2.5 align-top">
                        {canRoll ? (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={(e) => {
                              e.stopPropagation();
                              setSkillModal({ name: n, version: v });
                            }}
                          >
                            版本 / 回滚
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </Table>
            </ListViewport>
            <PaginationControls {...skillPager} onPageChange={skillPager.setPage} />
          </>
        )}
      </Panel>

      {/* modals */}
      <SkillVersionModal
        name={skillModal?.name ?? null}
        initialVersion={skillModal?.version ?? null}
        open={!!skillModal}
        onClose={() => setSkillModal(null)}
        onRolled={() => refresh(true)}
      />
      <SessionModal
        sid={sessModal?.sid ?? null}
        initialTab={sessModal?.tab ?? "detail"}
        open={!!sessModal}
        onClose={() => setSessModal(null)}
      />
      <CandidateModal
        jobId={candJobId}
        cand={openCand}
        ev={candJobId ? evalCache[candJobId] ?? null : null}
        evaluating={candJobId ? !!evaluating[candJobId] : false}
        open={!!candJobId}
        onClose={() => setCandJobId(null)}
        onEvaluate={(force) => candJobId && evaluate(candJobId, force)}
      />
    </div>
  );
}

// ---- Small helpers ---- //
function ConnBadge({ storage }: { storage: StorageStatus | null }) {
  if (!storage) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <Dot state="off" /> OpenViking：不可达
      </span>
    );
  }
  const ok = storage.reachable;
  const backend = (storage.backend || "?").toUpperCase();
  const label = backend === "VIKING" ? "OpenViking" : backend;
  const title = [
    storage.endpoint ? "endpoint=" + storage.endpoint : "",
    storage.namespace ? "namespace=" + storage.namespace : "",
    "api_key=" + (storage.api_key_present ? "有" : "无"),
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
      title={title}
    >
      <Dot state={ok ? "on" : "err"} /> {label}：{ok ? "已连接" : "不可达"}
    </span>
  );
}

function Table({
  headers,
  children,
}: {
  headers: string[];
  children: ReactNode;
}) {
  return (
    <table className="w-full border-collapse">
      <thead>
        <tr>
          {headers.map((h) => (
            <th
              key={h}
              className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground"
            >
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

function Td({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <td className={`border-b border-line px-4 py-2.5 align-top ${className || ""}`}>{children}</td>
  );
}
