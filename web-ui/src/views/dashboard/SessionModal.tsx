import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  UserBadge,
  Pill,
  Empty,
  ErrorText,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { cn } from "@/lib/utils";
import { fmtTime } from "@/lib/format";
import {
  api,
  type EvidenceClassification,
  type SessionDetail,
  type SessionProcess,
} from "@/api/client";

export type SessTab = "detail" | "process";

function StatusBadge({ status }: { status?: string }) {
  if (status === "consumed") return <Pill tone="green">已消费</Pill>;
  if (status === "queued") return <Pill tone="amber">排队中</Pill>;
  return <Pill tone="gray">{status || "-"}</Pill>;
}

function isNoActionNormal(c: NonNullable<SessionProcess["cycles"]>[number], evos: unknown[]) {
  return (
    !evos.length &&
    !c.had_processing_error &&
    Number(c.sessions || 0) > 0 &&
    Number(c.skill_groups || 0) > 0 &&
    Number(c.actions || 0) === 0 &&
    Number(c.uploaded_skills || 0) === 0 &&
    Number(c.candidates_queued || 0) === 0
  );
}

function EvidenceRouting({ value }: { value?: EvidenceClassification }) {
  const rows = [
    ["团队 SOP", value?.team_skill || []],
    ["用户 Memory 候选", value?.user_memory || []],
    ["当前任务要求", value?.task_requirement || []],
    ["运行时问题", value?.agent_runtime || []],
    ["证据不足", value?.insufficient_evidence || []],
  ] as const;
  const populated = rows.filter(([, items]) => items.length);
  if (!populated.length) return null;

  const describe = (item: string | Record<string, unknown>) => {
    if (typeof item === "string") return item;
    for (const key of ["claim", "preference", "requirement", "issue", "observation", "reason"]) {
      if (typeof item[key] === "string" && item[key]) return String(item[key]);
    }
    return JSON.stringify(item);
  };

  return (
    <div className="mt-2 rounded-md border border-border bg-background/70 p-2.5 text-xs">
      <div className="mb-1.5 font-semibold text-muted-foreground">证据归属</div>
      <div className="space-y-1.5">
        {populated.map(([label, items]) => (
          <div key={label}>
            <span className="font-medium">{label} ({items.length})</span>
            <span className="text-muted-foreground">：{items.slice(0, 2).map(describe).join("；")}</span>
          </div>
        ))}
      </div>
      {(value?.user_memory?.length || 0) > 0 && (
        <div className="mt-2 text-muted-foreground">
          此处仅为个人记忆候选，尚未写入用户 Memory。
        </div>
      )}
    </div>
  );
}

export default function SessionModal({
  sid,
  initialTab,
  open,
  onClose,
}: {
  sid: string | null;
  initialTab: SessTab;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<SessTab>(initialTab);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [process, setProcess] = useState<SessionProcess | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab, sid]);

  useEffect(() => {
    if (!open || !sid) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    const p =
      tab === "detail"
        ? api<SessionDetail>(`/conversations/${encodeURIComponent(sid)}`).then((d) => {
            if (!cancelled) setDetail(d);
          })
        : api<SessionProcess>(`/conversations/${encodeURIComponent(sid)}/process`).then(
            (d) => {
              if (!cancelled) setProcess(d);
            }
          );
    p.catch((e) => {
      if (!cancelled) setError(e.message);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open, sid, tab]);

  const title = detail?.meta?.title || "会话详情";

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[88vh] w-full !max-w-[860px] overflow-auto">
        <DialogHeader>
          <DialogTitle>{tab === "detail" ? title : "会话详情"}</DialogTitle>
        </DialogHeader>

        {/* tabs */}
        <div className="flex flex-wrap gap-1.5">
          {(["detail", "process"] as SessTab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md border px-3.5 py-1 text-xs font-semibold transition-colors",
                t === tab
                  ? "border-sidebar-primary bg-sidebar-primary text-white"
                  : "border-border bg-transparent hover:bg-muted"
              )}
            >
              {t === "detail" ? "会话内容" : "进化过程"}
            </button>
          ))}
        </div>

        {loading ? (
          <Empty>加载中…</Empty>
        ) : error ? (
          <ErrorText>加载失败：{error}</ErrorText>
        ) : tab === "detail" ? (
          <DetailBody d={detail} />
        ) : (
          <ProcessBody p={process} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function DetailBody({ d }: { d: SessionDetail | null }) {
  const m = d?.meta || {};
  const metrics = d?.metrics || {};
  const turns = d?.turns || [];
  const turnsPager = usePagedItems(turns);
  if (!d) return null;
  return (
    <div className="space-y-3 text-sm">
      <div>
        <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
          提交人 / 状态 / 轮数
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <UserBadge name={m.user_alias} />
          <span className="text-muted-foreground">·</span>
          <StatusBadge status={m.status} />
          <span className="text-muted-foreground">·</span>
          <span>{m.num_turns != null ? m.num_turns : "-"} 轮</span>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {[
          ["交互轮次", metrics.interaction_turns ?? m.num_turns ?? 0],
          ["工具调用", metrics.tool_call_count ?? 0],
          ["Total Tokens", metrics.total_tokens ?? 0],
          ["API 调用", metrics.api_call_count ?? 0],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-lg border border-border bg-surface-subtle p-2.5">
            <div className="text-[11px] font-semibold text-muted-foreground">{label}</div>
            <div className="mt-1 text-lg font-bold">{Number(value).toLocaleString()}</div>
          </div>
        ))}
      </div>
      <SkillPills title="Injected Skills（本轮系统提示词实际暴露）" skills={d.injected_skills || []} />
      <SkillPills title="Used Skills（实际调用 skill_view）" skills={d.used_skills || []} tone="green" />
      {d.system_prompt && (
        <details className="rounded-lg border border-border p-3">
          <summary className="cursor-pointer text-xs font-semibold">查看完整 System Prompt</summary>
          <pre className="mt-2 max-h-[320px] overflow-auto whitespace-pre-wrap text-[11px] text-muted-foreground">
            {d.system_prompt}
          </pre>
        </details>
      )}
      {!d.turns_available || !turns.length ? (
        <Empty>
          此会话已被消费且早于内容归档上线，仅存元数据（无正文可回看）。此后新消费的会话都会保留正文。
        </Empty>
      ) : (
        <>
          {d.turns_source === "archive" && (
            <div className="text-xs text-muted-foreground">正文来自消费后归档快照</div>
          )}
          <ListViewport maxHeight="560px">
            {turnsPager.items.map((t, i) => (
              <div key={`${t.turn_num ?? "turn"}-${turnsPager.start + i}`} className="mb-3">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  第 {t.turn_num != null ? t.turn_num : "?"} 轮
                </div>
                <SkillPills title="Injected" skills={t.injected_skills || []} compact />
                <SkillPills title="Used" skills={t.used_skills || []} tone="green" compact />
                {t.prompt_text && (
                  <div className="bubble user">
                    <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
                      👤 用户
                    </div>
                    {t.prompt_text}
                  </div>
                )}
                {t.response_text && (
                  <div className="bubble asst">
                    <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
                      🤖 助手
                    </div>
                    {t.response_text}
                  </div>
                )}
                {!!t.tool_calls?.length && (
                  <div className="mt-2 rounded-lg border border-border bg-background/60 p-2.5">
                    <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">
                      工具调用（{t.tool_calls.length}）
                    </div>
                    {t.tool_calls.map((call, callIndex) => (
                      <div key={call.id || callIndex} className="mb-1 font-mono text-[11px] break-all">
                        {call.function?.name || "unknown"}({String(call.function?.arguments || "")})
                      </div>
                    ))}
                  </div>
                )}
                {!!t.tool_results?.length && (
                  <details className="mt-2 rounded-lg border border-border p-2.5">
                    <summary className="cursor-pointer text-[11px] font-semibold">
                      Tool Results（{t.tool_results.length}）
                    </summary>
                    <div className="mt-2 space-y-2">
                      {t.tool_results.map((result, resultIndex) => (
                        <pre key={`${result.tool_call_id || "result"}-${resultIndex}`} className={cn("max-h-[180px] overflow-auto whitespace-pre-wrap text-[11px]", result.has_error && "text-destructive")}>
                          [{result.tool_name || "tool"}] {result.content || ""}
                        </pre>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            ))}
          </ListViewport>
          <PaginationControls {...turnsPager} onPageChange={turnsPager.setPage} />
        </>
      )}
    </div>
  );
}

function SkillPills({
  title,
  skills,
  tone = "blue",
  compact = false,
}: {
  title: string;
  skills: string[];
  tone?: "blue" | "green";
  compact?: boolean;
}) {
  if (!skills.length) return compact ? null : (
    <div className="text-xs text-muted-foreground">{title}：无</div>
  );
  return (
    <div className={compact ? "mb-2" : ""}>
      <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">{title}</div>
      <div className="flex flex-wrap gap-1.5">
        {skills.map((skill) => <Pill key={skill} tone={tone}>{skill}</Pill>)}
      </div>
    </div>
  );
}

function ProcessBody({ p }: { p: SessionProcess | null }) {
  const cycles = p?.cycles || [];
  const cyclesPager = usePagedItems(cycles);
  if (!cycles.length) {
    return (
      <Empty>
        该会话尚未进入任何已完成的进化周期（可能仍在排队，或所在周期未产生记录）。
      </Empty>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <ListViewport maxHeight="560px">
        {cyclesPager.items.map((c, i) => {
          const j = c.judge || {};
          const evos = c.evolutions || [];
          const noActionNormal = isNoActionNormal(c, evos);
          return (
            <div key={`${c.timestamp || "cycle"}-${cyclesPager.start + i}`} className="rounded-lg border border-border p-4">
            <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>
                🕑 {fmtTime(c.timestamp)} &nbsp;·&nbsp; 本周期 {c.sessions ?? "?"} 会话 /{" "}
                {c.skill_groups ?? "?"} 技能组 / 上传 {c.uploaded_skills ?? 0} / 候选{" "}
                {c.candidates_queued ?? 0}
              </span>
              {noActionNormal && <Pill tone="blue">无需进化</Pill>}
            </div>
            <div className="mb-3">
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
                会话评审
              </div>
              <div>
                {j.overall_score != null || j.rationale ? (
                  <>
                    {j.rationale ? (
                      <span className="text-muted-foreground">{j.rationale}</span>
                    ) : (
                      <span className="text-muted-foreground">已完成会话评审</span>
                    )}
                  </>
                ) : (
                  <span className="text-muted-foreground">本周期无该会话的评审明细</span>
                )}
              </div>
            </div>
            <div>
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
                本会话相关的技能进化
              </div>
              {evos.length ? (
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
                        <EvidenceRouting value={e.evidence_classification} />
                      </div>
                    );
                  })}
                </div>
              ) : (
                noActionNormal ? (
                  <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
                    <Pill tone="blue">流程正常</Pill>
                    <div className="mt-2">
                      该会话已完成评审与 Skill 分组，planner 判断当前 Skill 无需优化，也无需创建新 Skill。
                    </div>
                  </div>
                ) : (
                  <div className="text-xs text-muted-foreground">
                    本会话未直接触发技能变更（可能仅参与聚合评估）。
                  </div>
                )
              )}
            </div>
            </div>
          );
        })}
      </ListViewport>
      <PaginationControls {...cyclesPager} onPageChange={cyclesPager.setPage} />
    </div>
  );
}

export { StatusBadge };
