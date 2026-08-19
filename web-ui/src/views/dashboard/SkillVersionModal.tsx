import { useEffect, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Pill, Empty, ErrorText, ListViewport, PaginationControls, usePagedItems } from "@/components/common";
import UnifiedDiffView from "@/components/UnifiedDiffView";
import { cn } from "@/lib/utils";
import { api, type SkillVersionResp } from "@/api/client";
import { toastOk, toastErr } from "@/lib/toast";

const EFFICIENCY_LABELS: Record<string, string> = {
  interaction_turns: "交互轮次",
  tool_call_count: "工具调用",
  total_tokens: "Tokens",
};

export default function SkillVersionModal({
  name,
  initialVersion,
  open,
  onClose,
  onRolled,
}: {
  name: string | null;
  initialVersion: number | null;
  open: boolean;
  onClose: () => void;
  onRolled: () => void;
}) {
  const [version, setVersion] = useState<number | null>(initialVersion);
  const [data, setData] = useState<SkillVersionResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setVersion(initialVersion);
  }, [initialVersion, name]);

  useEffect(() => {
    if (!open || !name || version == null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api<SkillVersionResp>(
      `/api/skills/${encodeURIComponent(name)}/versions/${version}`
    )
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, name, version]);

  async function rollbackFromModal(target: number) {
    if (!name) return;
    if (
      !window.confirm(
        `确认将 ${name} 回滚到 v${target}？将以该版本内容发布为新版本。`
      )
    )
      return;
    try {
      const r = await api<{ new_version?: number }>(
        `/api/skills/${encodeURIComponent(name)}/rollback?target_version=${target}`,
        { method: "POST" }
      );
      toastOk("已回滚", name + (r.new_version ? ` → 新版本 v${r.new_version}` : ""));
      onClose();
      onRolled();
    } catch (e: any) {
      toastErr("回滚失败", e.message);
    }
  }

  const versions =
    data?.versions && data.versions.length ? data.versions : version != null ? [version] : [];
  const versionPager = usePagedItems(versions);
  const efficiencyDimensions =
    data?.evolution?.evaluation?.replay?.efficiency?.dimensions || {};
  const canRoll =
    data != null && version !== data.current_version && (data.current_version || 0) > 0;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[88vh] w-full !max-w-[860px] overflow-auto">
        <DialogHeader>
          <DialogTitle>技能详情 · {name}</DialogTitle>
        </DialogHeader>

        {loading && !data ? (
          <Empty>加载中…</Empty>
        ) : error ? (
          <ErrorText>加载失败：{error}</ErrorText>
        ) : data ? (
          <div className="space-y-4 text-sm">
            {/* version switcher */}
            <div>
              <span className="mr-1 text-xs text-muted-foreground">版本</span>
              <ListViewport maxHeight="120px">
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {versionPager.items.map((v) => (
                    <button
                      key={v}
                      onClick={() => setVersion(v)}
                      title={v === data.current_version ? "当前线上版本" : ""}
                      className={cn(
                        "rounded-md border px-2.5 py-0.5 text-xs transition-colors",
                        v === version
                          ? "border-sidebar-primary bg-sidebar-primary text-white"
                          : "border-border bg-transparent hover:bg-muted",
                        v === data.current_version && v !== version && "border-success"
                      )}
                    >
                      v{v}
                      {v === data.current_version ? " ·当前" : ""}
                    </button>
                  ))}
                </div>
              </ListViewport>
              <PaginationControls {...versionPager} onPageChange={versionPager.setPage} />
            </div>

            <Field k="Skill ID">
              <span className="mono">{data.skill_id || "-"}</span>
            </Field>
            <Field k="分类">{data.category || "general"}</Field>
            <Field k="描述">{data.description || "（无描述）"}</Field>
            {data.evolution && Object.keys(data.evolution).length > 0 && (
              <div className="rounded-lg border border-border bg-surface-subtle p-3">
                <div className="mb-2 text-xs font-semibold text-muted-foreground">
                  版本进化内容
                </div>
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <Pill tone="blue">
                    {data.evolution.proposed_action || "skill evolution"}
                  </Pill>
                  {data.evolution.job_id && (
                    <span className="mono text-[11px] text-muted-foreground">
                      {data.evolution.job_id}
                    </span>
                  )}
                </div>
                {data.evolution.optimization_items?.length ? (
                  <ul className="mb-3 list-disc space-y-1 pl-5 text-xs">
                    {data.evolution.optimization_items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : null}
                {data.evolution.rationale && (
                  <p className="mb-3 whitespace-pre-wrap text-xs text-muted-foreground">
                    {data.evolution.rationale}
                  </p>
                )}
                {data.evolution.skill_diff && (
                  <details className="mb-3" open>
                    <summary className="cursor-pointer text-xs font-semibold">
                      具体版本差异
                    </summary>
                      <UnifiedDiffView diff={data.evolution.skill_diff} className="mt-2 max-h-[280px]" />
                  </details>
                )}
                {data.evolution.bundle_diff?.files?.some((file) => file.status !== "unchanged") && (
                  <div className="mb-3 space-y-2">
                    <div className="text-xs font-semibold">技能包文件差异</div>
                    {data.evolution.bundle_diff.files
                      .filter((file) => file.status !== "unchanged")
                      .map((file) => (
                        <details key={file.path} className="rounded-md border border-border p-2.5">
                          <summary className="cursor-pointer text-xs font-semibold">
                            {file.status} · {file.path}
                          </summary>
                          {file.diff ? (
                              <UnifiedDiffView diff={file.diff} className="mt-2 max-h-[260px]" />
                          ) : (
                            <p className="mt-2 text-xs text-muted-foreground">
                              二进制文件，{file.old_size || 0} → {file.new_size || 0} bytes
                            </p>
                          )}
                        </details>
                      ))}
                  </div>
                )}
                {Object.keys(efficiencyDimensions).length > 0 && (
                  <div className="mb-3">
                    <div className="mb-2 text-xs font-semibold">
                      与上一版本的效率对比（A/B 回放）
                    </div>
                    <div className="overflow-hidden rounded-md border border-border">
                      <table className="w-full border-collapse text-xs">
                        <thead>
                          <tr className="bg-surface-subtle text-muted-foreground">
                            {["指标", "上一版本", "本版本", "变化", "结果"].map((label) => (
                              <th key={label} className="px-3 py-2 text-left font-semibold">
                                {label}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {Object.entries(efficiencyDimensions).map(([key, metric]) => (
                            <tr key={key} className="border-t border-border">
                              <td className="px-3 py-2 font-semibold">
                                {EFFICIENCY_LABELS[key] || key}
                              </td>
                              <td className="px-3 py-2">
                                {Number(metric.baseline || 0).toLocaleString()}
                              </td>
                              <td className="px-3 py-2">
                                {Number(metric.candidate || 0).toLocaleString()}
                              </td>
                              <td
                                className={cn(
                                  "px-3 py-2 font-bold",
                                  metric.delta > 0 && "text-success",
                                  metric.delta < 0 && "text-destructive"
                                )}
                              >
                                {metric.delta > 0 ? "-" : metric.delta < 0 ? "+" : ""}
                                {Math.abs(Number(metric.delta || 0)).toLocaleString()}
                              </td>
                              <td className="px-3 py-2">
                                <Pill
                                  tone={
                                    metric.winner === "candidate"
                                      ? "green"
                                      : metric.winner === "baseline"
                                        ? "red"
                                        : "gray"
                                  }
                                >
                                  {metric.winner === "candidate"
                                    ? "本版本更优"
                                    : metric.winner === "baseline"
                                      ? "上一版本更优"
                                      : "持平"}
                                </Pill>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
            <div>
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                技能包入口 SKILL.md
                <Pill tone={data.is_current ? "green" : "gray"}>
                  v{data.version}
                  {data.is_current ? " ·当前" : ""}
                </Pill>
              </div>
              <pre className="content">{data.content || data.raw_md || "（空）"}</pre>
            </div>

            {canRoll && version != null && (
              <div className="flex items-center gap-2">
                <Button onClick={() => rollbackFromModal(version)}>
                  回滚到 v{version}
                </Button>
                <span className="text-xs text-muted-foreground">
                  将以该版本完整技能包发布为新版本
                </span>
              </div>
            )}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function Field({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">{k}</div>
      <div>{children}</div>
    </div>
  );
}
