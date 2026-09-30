import { Eye, GitCompareArrows, History, LoaderCircle, PencilLine, Save, X } from "lucide-react";

import { Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type KnowledgeEditorMode = "edit" | "preview";

export function KnowledgeFileEditorToolbar({
  isMarkdown,
  mode,
  onModeChange,
  onVersion,
  editable,
  dirty,
  saving,
  onSave,
}: {
  isMarkdown: boolean;
  mode: KnowledgeEditorMode;
  onModeChange: (mode: KnowledgeEditorMode) => void;
  onVersion: () => void;
  editable: boolean;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      {isMarkdown && (
        <div className="flex rounded-lg border border-border bg-surface-subtle p-0.5">
          <button
            type="button"
            className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", mode === "edit" && "bg-white shadow-sm")}
            onClick={() => onModeChange("edit")}
          >
            <PencilLine className="size-3" /> 编辑
          </button>
          <button
            type="button"
            className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", mode === "preview" && "bg-white shadow-sm")}
            onClick={() => onModeChange("preview")}
          >
            <Eye className="size-3" /> 预览
          </button>
        </div>
      )}
      <Button variant="outline" size="sm" onClick={onVersion}>
        <History className="size-3.5" /> 版本
      </Button>
      {editable ? (
        <Button size="sm" disabled={!dirty || saving} onClick={onSave}>
          {saving ? <LoaderCircle className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
          {saving ? "保存中" : "保存"}
        </Button>
      ) : <Pill tone="gray">只读</Pill>}
    </div>
  );
}

export function KnowledgeVersionPanel({
  open,
  name,
  uri,
  onClose,
}: {
  open: boolean;
  name: string;
  uri: string;
  onClose: () => void;
}) {
  if (!open || !name) return null;
  return (
    <div
      className="fixed inset-0 z-[80] flex justify-end bg-black/45"
      role="dialog"
      aria-modal="true"
      aria-label={`${name} 版本历史`}
      onMouseDown={onClose}
    >
      <aside
        className="flex h-full w-[min(920px,78vw)] min-w-[640px] flex-col bg-white shadow-2xl"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="flex h-[68px] shrink-0 items-center gap-3 border-b border-line px-5">
          <span className="grid size-9 place-items-center rounded-xl bg-surface-subtle text-accent">
            <History className="size-5" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-base font-bold" title={`版本历史 · ${name}`}>版本历史 · {name}</div>
            <code className="mt-0.5 block truncate text-[10px] text-muted-foreground" title={uri}>{uri}</code>
          </div>
          <button
            type="button"
            className="grid size-9 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-surface-subtle hover:text-foreground"
            aria-label="关闭版本历史"
            onClick={onClose}
          >
            <X className="size-5" />
          </button>
        </header>

        <div className="grid min-h-0 flex-1 grid-cols-[260px_minmax(0,1fr)]">
          <section className="flex min-h-0 flex-col border-r border-line bg-[#fbfcfe]">
            <div className="border-b border-line px-5 py-3 text-xs font-bold">版本记录</div>
            <div className="grid min-h-0 flex-1 place-items-center px-5 text-center">
              <div>
                <History className="mx-auto mb-3 size-8 text-muted-soft" />
                <div className="text-xs font-semibold text-foreground">暂无版本记录</div>
              </div>
            </div>
          </section>

          <section className="flex min-h-0 flex-col">
            <div className="border-b border-line px-6 py-3">
              <div className="text-xs font-bold">版本差异</div>
              <div className="mt-0.5 text-[10px] text-muted-foreground">选择两个版本后查看文件变更</div>
            </div>
            <div className="grid min-h-0 flex-1 place-items-center px-8 text-center">
              <div className="max-w-sm">
                <GitCompareArrows className="mx-auto mb-4 size-10 text-muted-soft" />
                <div className="text-sm font-bold text-foreground">版本管理暂不可用</div>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">
                  OpenViking 暂未提供文件版本接口。接口可用后，这里将显示版本列表和版本差异。
                </p>
              </div>
            </div>
          </section>
        </div>
      </aside>
    </div>
  );
}
