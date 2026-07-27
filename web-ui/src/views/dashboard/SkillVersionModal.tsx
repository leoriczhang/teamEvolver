import { useEffect, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Pill, Empty, ErrorText, ListViewport, PaginationControls, usePagedItems } from "@/components/common";
import { cn } from "@/lib/utils";
import { api, type SkillVersionResp } from "@/api/client";
import { toastOk, toastErr } from "@/lib/toast";

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
            <div>
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                SKILL.md 内容
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
                  将以该版本内容发布为新版本
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
