import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Panel,
  StatCard,
  Pill,
  Dot,
  Empty,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { toastOk, toastErr } from "@/lib/toast";
import {
  api,
  hydrateCandidates,
  mergeCandidateFeedback,
  tenantHeaders,
  type CandidateFeedback,
  type StatusResp,
  type StorageStatus,
  type Candidate,
  type EvalResult,
  type PageResponse,
} from "@/api/client";
import { useSessionList, type SessionListFilters } from "@/hooks/useSessionList";
import SessionTable from "@/components/session/SessionTable";
import SessionFiltersBar from "@/components/session/SessionFilters";
import SkillVersionModal from "./dashboard/SkillVersionModal";
import SkillEditModal from "./skills/SkillEditModal";
import SessionModal, { type SessTab } from "./dashboard/SessionModal";
import CandidateModal from "./dashboard/CandidateModal";
import CandidateFeedbackControls from "./dashboard/CandidateFeedback";
import SetupChecklist from "./dashboard/SetupChecklist";
import DatasetSelectionDialog, { type DatasetSelection } from "./datasets/DatasetSelectionDialog";

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

export default function DashboardView({
  active,
  onNavigate,
}: {
  active: boolean;
  /** Switch to another console view (used by the first-run setup checklist). */
  onNavigate: (view: string) => void;
}) {
  const [status, setStatus] = useState<StatusResp | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [cands, setCands] = useState<Candidate[]>([]);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [candidatePage, setCandidatePage] = useState(1);
  const [lastUpdate, setLastUpdate] = useState("—");
  // Session filters start from the URL (shareable/bookmarkable views) and are
  // written back on every change via history.replaceState.
  const FILTER_URL_KEYS = [
    "search",
    "status",
    "decision",
    "case",
    "skill",
    "start",
    "end",
    "sort_by",
    "order",
  ] as const;
  const readFiltersFromUrl = (): SessionListFilters => {
    const p = new URLSearchParams(window.location.search);
    const f: SessionListFilters = {};
    for (const k of FILTER_URL_KEYS) {
      const v = p.get(k);
      if (v) (f as Record<string, string>)[k] = v;
    }
    return f;
  };
  const [sessFilters, setSessFilters] = useState<SessionListFilters>(readFiltersFromUrl);
  const [selectedSessIds, setSelectedSessIds] = useState<string[]>([]);
  const [datasetSelection, setDatasetSelection] = useState<{ selection: DatasetSelection; count: number } | null>(null);
  const [exportFormat, setExportFormat] = useState<"csv" | "json">("csv");

  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    for (const k of FILTER_URL_KEYS) p.delete(k);
    for (const [k, v] of Object.entries(sessFilters)) {
      if (v) p.set(k, String(v));
    }
    const qs = p.toString();
    window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessFilters]);

  const sessions = useSessionList({
    filters: sessFilters,
    pageSize: PAGE_SIZE,
    pollMs: POLL_MS,
    enabled: active,
  });
  const [evalCache, setEvalCache] = useState<Record<string, EvalResult>>({});
  const [evaluating, setEvaluating] = useState<Record<string, boolean>>({});
  const [backfilling, setBackfilling] = useState(false);
  // Skill management modal: editName null=create, string=edit.
  const [skillEditName, setSkillEditName] = useState<string | null | undefined>(undefined);

  // Sweep archived sessions lacking a quality score into the server's async
  // judge worker (fills historical task_only/chitchat sessions that never ran
  // through an evolution cycle). Scores then appear via the normal 15s poll.
  const backfillJudges = useCallback(async () => {
    if (backfilling) return;
    if (!window.confirm("将对本租户尚无评审分、且有正文的会话批量补评（后台异步、有速率上限）。继续？")) {
      return;
    }
    setBackfilling(true);
    try {
      const r = await api<{ enqueued: number; backlog?: number }>(
        "/conversations/judge-backfill",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ limit: 200 }),
        }
      );
      toastOk(`已加入补评队列 ${r.enqueued} 个会话`, "评审在后台进行，稍后自动刷新出现分数");
      sessions.reload();
    } catch (e: any) {
      toastErr("发起补评失败", e?.message || String(e));
    } finally {
      setBackfilling(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backfilling]);

  const inflight = useRef(false);
  const evaluatingRef = useRef<Record<string, boolean>>({});
  const evalCacheRef = useRef<Record<string, EvalResult>>({});
  // job_id -> last loaded full candidate detail; polled compact list items
  // are rehydrated from this so an open modal never loses its content.
  const candDetailRef = useRef<Record<string, Candidate>>({});
  evaluatingRef.current = evaluating;
  evalCacheRef.current = evalCache;

  // modal state
  const [skillModal, setSkillModal] = useState<{ name: string; version: number } | null>(null);
  const [sessModal, setSessModal] = useState<{ sid: string; tab: SessTab } | null>(null);
  const [candJobId, setCandJobId] = useState<string | null>(null);

  const loadCandidateDetail = useCallback(async (jobId: string) => {
    try {
      const fetched = await api<Candidate>(
        `/api/skill-candidates/${encodeURIComponent(jobId)}/detail`
      );
      const savedFeedback = candDetailRef.current[jobId]?.feedback;
      const detail = savedFeedback ? mergeCandidateFeedback(fetched, savedFeedback) : fetched;
      candDetailRef.current[jobId] = detail;
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

  const handleFeedbackSaved = useCallback((jobId: string, feedback: CandidateFeedback) => {
    candDetailRef.current[jobId] = mergeCandidateFeedback(candDetailRef.current[jobId] ?? { job_id: jobId }, feedback);
    setCands((items) => items.map((item) => item.job_id === jobId ? mergeCandidateFeedback(item, feedback) : item));
  }, []);

  const evaluate = useCallback(async (jobId: string, force: boolean) => {
    if (evaluatingRef.current[jobId]) return;
    setEvaluating((m) => ({ ...m, [jobId]: true }));
    try {
      const r = await api<EvalResult & { status?: string }>(
        `/api/skill-candidates/${encodeURIComponent(jobId)}/evaluate${force ? "?refresh=true" : ""}`,
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

  // Batch export via the standalone /conversations/export endpoint.
  // "selected" exports checked sessions; "filtered" reuses the current
  // server-side filters so the export matches exactly what the list shows.
  const handleExport = useCallback(
    async (mode: "selected" | "filtered") => {
      const qs = new URLSearchParams();
      qs.set("format", exportFormat);
      if (mode === "selected") {
        if (!selectedSessIds.length) return;
        qs.set("ids", selectedSessIds.join(","));
      } else {
        for (const [k, v] of Object.entries(sessFilters)) {
          if (v && k !== "sort_by" && k !== "order") qs.set(k, String(v));
        }
      }
      const url = `/conversations/export?${qs.toString()}`;
      try {
        const res = await fetch(url, { headers: tenantHeaders(url) });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const blob = await res.blob();
        const ext = exportFormat === "csv" ? "csv" : "json";
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `sessions_export.${ext}`;
        a.click();
        URL.revokeObjectURL(a.href);
        const count =
          Number(res.headers.get("X-Export-Count")) ||
          (mode === "selected" ? selectedSessIds.length : sessions.total);
        toastOk(`已导出 ${count} 个会话（${ext.toUpperCase()}）`);
      } catch (e: any) {
        toastErr("导出失败", e?.message || String(e));
      }
    },
    [exportFormat, selectedSessIds, sessFilters, sessions.total]
  );

  const refresh = useCallback(
    async (force: boolean) => {
      if (inflight.current && !force) return;
      inflight.current = true;
      const refreshFlag = force ? "&refresh=true" : "";
      const [statusResult, storageResult, candidateResult] = await Promise.allSettled([
        api<StatusResp>(`/status${force ? "?refresh=true" : ""}`),
        api<StorageStatus>("/storage/status"),
        api<PageResponse<Candidate>>(
          `/api/skill-candidates?compact=true&limit=${PAGE_SIZE}&offset=${(candidatePage - 1) * PAGE_SIZE}${refreshFlag}`
        ),
      ]);
      const st = statusResult.status === "fulfilled" ? statusResult.value : null;
      const sto = storageResult.status === "fulfilled" ? storageResult.value : null;
      const candidatePayload = candidateResult.status === "fulfilled" ? candidateResult.value : null;
      const cs = candidatePayload?.candidates || null;
      const nextCands = hydrateCandidates(cs || [], candDetailRef.current);
      const serverEvaluations: Record<string, EvalResult> = {};
      for (const c of nextCands) {
        if (c.evaluation) serverEvaluations[c.job_id] = c.evaluation;
      }
      const mergedCache = { ...evalCacheRef.current, ...serverEvaluations };
      if (Object.keys(serverEvaluations).length) {
        setEvalCache((m) => ({ ...m, ...serverEvaluations }));
      }
      // Keep last good data on partial failure: a transient backend stall
      // (single event loop shared with the evolution cycle) must never blank
      // the page — only successful payloads update state. An EMPTY list is
      // treated as suspicious too: under load the backend may briefly fail
      // to read the candidate store and answer 200 with zero items; accept
      // it only when we have nothing to lose (initial load).
      if (st) setStatus(st);
      if (sto) setStorage(sto);
      if (cs === null) {
        // request failed — keep previous list
      } else if (cs.length === 0 && cands.length > 0) {
        // suspicious empty response while a list is on screen — keep old data
      } else {
        setCands(nextCands);
      }
      setCandidateTotal(candidatePayload?.total ?? candidateTotal);
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
    [candidatePage, cands.length, evaluate]
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
        ? "确认强制发布该候选技能？（仍保留 True Replay 三项指标）"
        : "确认按 True Replay 发布？轮次下降直接正向；轮次持平时再比较工具调用和 Token。";
    if (!window.confirm(msg)) return;
    try {
      const r = await api<{ status?: string; version?: number; feedback?: CandidateFeedback }>(
        `/api/skill-candidates/${encodeURIComponent(jobId)}/validate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }
      );
      if (r.feedback) handleFeedbackSaved(jobId, r.feedback);
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

  async function rejectCandidate(jobId: string) {
    if (!window.confirm("确认驳回该候选？候选与评估记录将保留在历史中。")) return;
    try {
      const result = await api<{ feedback?: CandidateFeedback }>(
        `/api/skill-candidates/${encodeURIComponent(jobId)}/reject`,
        { method: "POST" }
      );
      if (result.feedback) handleFeedbackSaved(jobId, result.feedback);
      setEvalCache((m) => {
        const n = { ...m };
        delete n[jobId];
        return n;
      });
      if (candJobId === jobId) setCandJobId(null);
      toastOk("已驳回候选");
      refresh(true);
    } catch (e: any) {
      toastErr("驳回失败", e.message);
    }
  }

  const running = status?.running;
  const skills = status?.skills || {};
  const skillNames = Object.keys(skills);
  const openCand = candJobId ? cands.find((c) => c.job_id === candJobId) || null : null;
  const skillPager = usePagedItems(skillNames);
  const candPager = serverPager(candidatePage, candidateTotal, cands.length);
  const sessPager = serverPager(sessions.page, sessions.total, sessions.rows?.length || 0);

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      {/* header */}
      <div className="content-toolbar">
        <div className="flex items-center gap-2 text-[12px] font-[700] text-[#464c5e]">
          <Dot state={status ? (running ? "run" : "on") : "err"} />
          {status ? (running ? "进化任务运行中" : "进化服务空闲") : "进化服务不可达"}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ConnBadge storage={storage} />
          <span className="text-xs text-muted-foreground">{lastUpdate}</span>
          <Button
            variant="outline"
            size="sm"
            disabled={backfilling}
            title="对本租户尚无评审分的历史会话在后台批量补评"
            onClick={backfillJudges}
          >
            {backfilling ? "补评中…" : "补评历史会话"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              refresh(true);
              sessions.reload();
            }}
          >
            刷新
          </Button>
        </div>
      </div>

      {/* stats */}
      <div className="mb-[18px] grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="运行状态" value={status ? (running ? "进化中" : "空闲") : "不可达"} />
        <StatCard label="排队会话" value={status ? status.pending_sessions : "—"} />
        <StatCard label="已注册技能" value={status ? status.registered_skills : "—"} />
        <StatCard
          label="存储连接"
          value={storage ? (storage.reachable ? "正常" : "异常") : "—"}
        />
      </div>

      {/* first-run setup guidance (hidden once every step is done) */}
      <SetupChecklist active={active} onNavigate={onNavigate} />

      {datasetSelection && <DatasetSelectionDialog
        selection={datasetSelection.selection} selectionCount={datasetSelection.count}
        onClose={() => setDatasetSelection(null)}
        onSaved={() => { setDatasetSelection(null); onNavigate("datasets"); }}
      />}
      {/* sessions (unified list) */}
      <Panel
        title="会话"
        count={
          sessions.rows
            ? `${sessions.total} 条`
            : sessions.loading
              ? "加载中…"
              : sessions.error
                ? ""
                : "0 条"
        }
      >
        <SessionFiltersBar
          value={sessFilters}
          onApply={setSessFilters}
          skillOptions={sessions.skillCounts}
          actions={
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={!selectedSessIds.length}
                onClick={() => setDatasetSelection({ selection: { session_ids: [...selectedSessIds] }, count: selectedSessIds.length })}>
                选中存为数据集
              </Button>
              <Button variant="outline" size="sm" disabled={!sessions.total}
                onClick={() => setDatasetSelection({ selection: { filters: { ...sessFilters } }, count: sessions.total })}>
                筛选存为数据集
              </Button>
              <select
                value={exportFormat}
                onChange={(e) => setExportFormat(e.target.value as "csv" | "json")}
                className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
                title="导出文件格式"
              >
                <option value="csv">CSV (Excel)</option>
                <option value="json">JSON</option>
              </select>
              <Button
                variant="outline"
                size="sm"
                disabled={!selectedSessIds.length}
                title={selectedSessIds.length ? "导出勾选的会话" : "先在列表中勾选会话"}
                onClick={() => handleExport("selected")}
              >
                导出选中 ({selectedSessIds.length})
              </Button>
              <Button
                variant="outline"
                size="sm"
                title={`按当前筛选条件批量导出，将导出 ${sessions.total} 条`}
                onClick={() => handleExport("filtered")}
              >
                导出筛选结果 ({sessions.total})
              </Button>
              {selectedSessIds.length > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  title="清空勾选"
                  onClick={() => setSelectedSessIds([])}
                >
                  清空勾选
                </Button>
              )}
            </div>
          }
        />
        {sessions.stats && (
          <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs text-muted-foreground">
            <span>
              共 <span className="font-semibold text-foreground">{sessions.stats.total}</span> 条
            </span>
            <span>
              Good <span className="font-semibold text-green-600">{sessions.stats.good}</span> ·
              Bad <span className="font-semibold text-red-600">{sessions.stats.bad}</span>
            </span>
            <span>
              有价值 <span className="font-semibold text-foreground">{sessions.stats.valuable}</span>{" "}
              · 闲聊 <span className="font-semibold text-foreground">{sessions.stats.chitchat}</span>
            </span>
            <span>
              平均分{" "}
              <span className="font-mono font-semibold text-foreground">
                {sessions.stats.avg_score != null ? sessions.stats.avg_score.toFixed(2) : "—"}
              </span>
            </span>
          </div>
        )}
        <SessionTable
          rows={sessions.rows}
          emptyText={
            sessions.loading
              ? "会话加载中…（新租户首次加载可能需要数秒）"
              : sessions.error
                ? "会话列表暂不可用（自动重试中）：" + sessions.error
                : "暂无会话"
          }
          onOpen={(sid, tab) => setSessModal({ sid, tab })}
          selectedIds={selectedSessIds}
          onToggleSelect={(sid, checked) =>
            setSelectedSessIds((prev) =>
              checked ? [...new Set([...prev, sid])] : prev.filter((id) => id !== sid)
            )
          }
          onSelectAll={(checked) =>
            setSelectedSessIds((prev) => {
              const pageIds = (sessions.rows || [])
                .map((r) => r.session_id)
                .filter(Boolean) as string[];
              if (checked) {
                return [...new Set([...prev, ...pageIds])];
              }
              return prev.filter((id) => !pageIds.includes(id));
            })
          }
        />
        <PaginationControls {...sessPager} onPageChange={sessions.setPage} />
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
                  "回放状态",
                  "客观结论",
                  "用户标记",
                  "操作",
                ]}
              >
                {cands.map((c) => {
              const ev = evalCache[c.job_id];
              const busy = evaluating[c.job_id];
              const open = () => openCandidate(c.job_id);
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
                      <Pill tone="amber">评估中…</Pill>
                    ) : ev ? (
                      <Pill tone="blue">已完成</Pill>
                    ) : (
                      <Pill tone="gray">待评估</Pill>
                    )}
                  </td>
                  <td className="link border-b border-line px-4 py-2.5 align-top" onClick={open}>
                    {ev ? (
                      ev.recommended_publish ? (
                        <Pill tone="green">指标改善</Pill>
                      ) : (
                        <Pill tone="amber">持平 / 有增加</Pill>
                      )
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                  <Td>
                    <CandidateFeedbackControls jobId={c.job_id} feedback={c.feedback} onSaved={handleFeedbackSaved} />
                  </Td>
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
                        按回放发布
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => validate(c.job_id, "force")}>
                        强制发布
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => rejectCandidate(c.job_id)}
                      >
                        驳回
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
          <div className="flex w-full items-center justify-between">
            <span>
              技能版本{" "}
              <span className="text-xs font-normal text-muted-foreground">
                （点击行查看详情 / 切换版本）
              </span>
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setSkillEditName(null)}
              >
                + 新建技能
              </Button>
            </div>
          </div>
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
                        <div className="flex items-center gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={(e) => {
                              e.stopPropagation();
                              setSkillEditName(n);
                            }}
                          >
                            编辑
                          </Button>
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
                        </div>
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
      <SkillEditModal
        name={skillEditName}
        open={skillEditName !== undefined}
        onClose={() => setSkillEditName(undefined)}
        onSaved={() => refresh(true)}
      />
      <SessionModal
        sid={sessModal?.sid ?? null}
        initialTab={sessModal?.tab ?? "detail"}
        open={!!sessModal}
        onClose={() => setSessModal(null)}
      />
      <CandidateModal
        onFeedbackSaved={handleFeedbackSaved}
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
        <Dot state="off" /> 存储：检查中
      </span>
    );
  }
  const ok = storage.reachable;
  const paused = !ok && storage.reason === "sharing_disabled";
  const backend = storage.pg ? "PostgreSQL" : (storage.backend || "?").toUpperCase();
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
      <Dot state={ok ? "on" : paused ? "off" : "err"} /> {label}：
      {ok ? "已连接" : paused ? "同步已暂停" : "不可达"}
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
