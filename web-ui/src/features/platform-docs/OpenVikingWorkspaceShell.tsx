import { Empty,Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import {
Dialog,
DialogContent,
DialogFooter,
DialogHeader,
DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { DiffView,diffStats } from "@/components/WorkspaceDiffView";
import { type UserProfile } from "@/features/admin/api/client";
import SkillTransferModal from "@/features/skills/skills/SkillTransferModal";
import { cn } from "@/lib/utils";
import {
Code2,
ExternalLink,
Eye,
FolderOpen,
GitCompare,
Loader2,
Pencil,
RefreshCw,
Save,
Search,
Trash2,
X
} from "lucide-react";
import { DirectoryLevelPreview,FilePreview,OpenVikingTerminal,SCOPE_LABELS,TreeRow,fileKind } from "./OpenVikingWorkspaceShellShared";
import { useOpenVikingWorkspaceShell } from "./useOpenVikingWorkspaceShell";

export default function OpenVikingWorkspaceShell({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
const { users, activeUserId, setActiveUserId, config, spaces, activeSpaceKey, entries, currentUri, setCurrentUri, selected, content, originalContent, viewMode, setViewMode, directoryLevel, setDirectoryLevel, directoryLevels, directoryLevelLoading, filter, setFilter, expanded, loading, saving, editing, reviewOpen, setReviewOpen, transfer, setTransfer, activeSpace, spaceScopes, selectedScope, draftList, dirtyUris, isDirty, canEditSelected, hasEditableScopes, activeWritableScope, terminalScope, filteredTree, loadConfig, chooseSpace, refreshTree, openEntry, updateDraftContent, beginEditing, discardEditing, finishEditing, saveDrafts } = useOpenVikingWorkspaceShell({ active, user });
return (
    <div className="w-full px-4 pb-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border bg-surface px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {spaces.map((space) => (
              <button
                key={space.key}
                type="button"
                onClick={() => void chooseSpace(space)}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors",
                  activeSpaceKey === space.key
                    ? "bg-sidebar-primary text-white"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {space.label}
              </button>
          ))}
          <span className="mx-1 h-5 w-px bg-border" />
          <Pill tone={editing ? "amber" : "gray"}>
            {editing ? "编辑模式 · " + draftList.length + " 个改动" : "浏览模式 · 只读"}
          </Pill>
          {selectedScope && <Pill tone="blue">{SCOPE_LABELS[selectedScope.name]}</Pill>}
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {selected?.uri || currentUri || activeSpace.label}
          </span>
        </div>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1.5">
          {users.length > 1 && (
            <select
              value={activeUserId}
              disabled={editing}
              title={editing ? "请先完成或取消当前编辑" : "切换用户"}
              onChange={(event) => {
                setActiveUserId(event.target.value);
                window.localStorage.setItem("teamEvolver.activeUserId", event.target.value);
                void loadConfig(event.target.value);
              }}
              className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold"
            >
              {users.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.display_name || item.id}
                </option>
              ))}
            </select>
          )}
          {editing ? (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={saving}
                  onClick={() => void discardEditing()}
                >
                  <X className="size-4" />
                  取消编辑
                </Button>
                <Button size="sm" disabled={saving} onClick={finishEditing}>
                  <GitCompare className="size-4" />
                  {draftList.length ? "完成编辑 " + draftList.length : "完成编辑"}
                </Button>
              </>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={!hasEditableScopes}
                title={hasEditableScopes ? "编辑多个 Memory 或 Skill 文件" : "当前空间没有可编辑资产"}
                onClick={beginEditing}
              >
                <Pencil className="size-4" />
                编辑
              </Button>
          )}
          {user?.role === "admin" && !editing && (
            <>
              <Button variant="outline" size="sm" onClick={() => setTransfer("import")}>导入 Skill</Button>
              <Button variant="outline" size="sm" onClick={() => setTransfer("export")}>导出 Skill</Button>
            </>
          )}
          {config?.studio_url && (
            <Button asChild variant="outline" size="sm">
              <a href={config.studio_url} target="_blank" rel="noreferrer">
                OpenViking Studio <ExternalLink />
              </a>
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => void refreshTree()}
          >
            <RefreshCw className={loading ? "animate-spin" : ""} />
            刷新
          </Button>
        </div>
      </div>

      {!config?.enabled ? (
        <div className="p-8">
          <Empty>
            请先在“运行状态”中启用 OpenViking，并配置本地部署或云端 endpoint。
          </Empty>
        </div>
      ) : (
        <div
          className="grid min-h-[560px] overflow-hidden rounded-xl border border-border bg-surface shadow-sm xl:grid-cols-[minmax(270px,0.78fr)_minmax(420px,1.35fr)_minmax(330px,0.9fr)]"
          style={{ height: "clamp(560px, calc(100vh - 176px), 840px)" }}
        >
        <section className="flex min-h-0 flex-col border-r border-border">
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-3">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <FolderOpen className="size-4 text-amber-600" />
              文件树
              <span className="text-[11px] font-normal text-muted-foreground">
                  {entries.length}
              </span>
            </div>
              {editing && (
                <Pill tone="amber">{draftList.length ? draftList.length + " 个文件已修改" : "等待修改"}</Pill>
              )}
          </header>
          <div className="shrink-0 border-b border-border p-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="搜索文件、路径或摘要"
                className="h-8 pl-8 pr-8 text-xs"
              />
              {filter && (
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  onClick={() => setFilter("")}
                >
                  <X className="size-3.5" />
                </button>
              )}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto py-1.5">
            <button
              type="button"
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs font-semibold hover:bg-muted/70"
              onClick={() => {
                  if (spaceScopes[0]?.root_uri) setCurrentUri(spaceScopes[0].root_uri);
              }}
            >
              <FolderOpen className="size-4 shrink-0 text-amber-600" />
                <span className="truncate">{activeSpace.label}</span>
            </button>
              {loading && !entries.length ? (
              <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                加载文件树…
              </div>
            ) : filteredTree.length ? (
              filteredTree.map((node) => (
                <TreeRow
                  key={node.entry.uri}
                  node={node}
                  depth={1}
                  expanded={expanded}
                  forceExpanded={!!filter.trim()}
                  selectedUri={selected?.uri || ""}
                  currentUri={currentUri}
                  dirtyUris={dirtyUris}
                  onOpen={openEntry}
                />
              ))
            ) : (
              <Empty>{filter ? "没有匹配的文件。" : "当前空间为空。"}</Empty>
            )}
          </div>
          <footer className="shrink-0 border-t border-border px-3 py-2 text-[10px] text-muted-foreground">
            {currentUri}
          </footer>
        </section>

        <section className="flex min-h-0 flex-col border-r border-border">
          <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-border px-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">
                {selected?.name || "内容预览"}
              </div>
              <div className="truncate font-mono text-[10px] text-muted-foreground">
                {selected?.uri || "从左侧选择文件"}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {selected?.is_dir ? (
                <div className="flex rounded-lg bg-muted p-0.5">
                  <button
                    type="button"
                    className={cn(
                      "flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold",
                      directoryLevel === "l0" && "bg-background shadow-sm",
                    )}
                    onClick={() => setDirectoryLevel("l0")}
                  >
                    L0 摘要
                  </button>
                  <button
                    type="button"
                    className={cn(
                      "flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold",
                      directoryLevel === "l1" && "bg-background shadow-sm",
                    )}
                    onClick={() => setDirectoryLevel("l1")}
                  >
                    L1 概览
                  </button>
                </div>
              ) : selected ? (
                <div className="flex rounded-lg bg-muted p-0.5">
                  <button
                    type="button"
                    className={cn(
                      "flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold",
                      viewMode === "preview" && "bg-background shadow-sm",
                    )}
                    onClick={() => setViewMode("preview")}
                  >
                    <Eye className="size-3.5" /> 预览
                  </button>
                  <button
                    type="button"
                    className={cn(
                      "flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold",
                      viewMode === "source" && "bg-background shadow-sm",
                    )}
                    onClick={() => setViewMode("source")}
                  >
                    <Code2 className="size-3.5" /> 源码
                  </button>
                  {editing && <button
                    type="button"
                    title={isDirty ? "查看与已保存版本的行级差异" : "草稿与已保存版本一致"}
                    className={cn(
                      "flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold",
                      viewMode === "diff" && "bg-background shadow-sm",
                      !isDirty && "opacity-50",
                    )}
                    onClick={() => setViewMode("diff")}
                  >
                    <GitCompare className="size-3.5" /> 差异
                    {isDirty && (
                      <span className="ml-0.5 size-1.5 rounded-full bg-amber-500" />
                    )}
                  </button>}
                </div>
              ) : null}
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-hidden bg-background">
            {!selected ? (
              <div className="flex h-full items-center justify-center p-8">
                <Empty>选择文件查看渲染预览或源码；窗口内容可独立滚动。</Empty>
              </div>
            ) : selected.is_dir ? (
              <DirectoryLevelPreview
                level={directoryLevel}
                content={directoryLevels[directoryLevel]}
                loading={directoryLevelLoading}
              />
            ) : viewMode === "source" ? (
              <Textarea
                value={content}
                readOnly={!canEditSelected}
                aria-label={canEditSelected ? "编辑文件内容" : "只读文件内容"}
                onChange={(event) => updateDraftContent(event.target.value)}
                className={cn(
                  "h-full min-h-0 resize-none rounded-none border-0 p-4 font-mono text-xs leading-5 focus-visible:ring-0",
                  !canEditSelected && "cursor-default bg-muted/20",
                )}
              />
            ) : viewMode === "diff" ? (
              <DiffView original={originalContent} next={content} />
            ) : (
              <FilePreview entry={selected} content={content} />
            )}
          </div>
          <footer className="flex h-8 shrink-0 items-center justify-between border-t border-border px-3 text-[10px] text-muted-foreground">
            <span>
              {selected?.is_dir
                ? directoryLevel === "l0"
                  ? "OpenViking L0 Abstract"
                  : "OpenViking L1 Overview"
                : fileKind(selected?.name || "")}
            </span>
            <span className="flex items-center gap-2">
              {isDirty && <span className="font-semibold text-amber-700">已加入变更集</span>}
              {(selected?.is_dir
                ? directoryLevels[directoryLevel].length
                : content.length
              ).toLocaleString()}{" "}
              字符
            </span>
          </footer>
        </section>

        <OpenVikingTerminal
            currentUri={currentUri || selectedScope?.root_uri || activeWritableScope?.root_uri || ""}
          config={config}
            scopeName={terminalScope}
          userId={activeUserId}
          onOpenUri={(uri) => {
              const entry = entries.find((item) => item.uri === uri);
            if (entry) void openEntry(entry);
          }}
          onRefresh={() => void refreshTree()}
        />
        </div>
      )}

      <Dialog
        open={reviewOpen}
        onOpenChange={(open) => {
          if (!saving) setReviewOpen(open);
        }}
      >
        <DialogContent
          showCloseButton={!saving}
          className="flex h-[88vh] w-full !max-w-[1180px] flex-col gap-0 overflow-hidden p-0"
        >
          <DialogHeader className="shrink-0 border-b border-line px-5 py-4">
            <DialogTitle className="flex flex-wrap items-center gap-2">
              <GitCompare className="size-4" />
              工作区改动
              <Pill tone="amber">{draftList.length} 个文件</Pill>
            </DialogTitle>
            <p className="text-xs text-muted-foreground">
              确认后统一写入 OpenViking；如果文件已被其他人更新，本次提交会被拒绝并保留草稿。
            </p>
          </DialogHeader>
          <div className="min-h-0 flex-1 space-y-3 overflow-auto bg-muted/30 p-4">
            {draftList.map((draft, index) => {
              const stats = diffStats(draft.originalContent, draft.content);
              return (
                <section
                  key={draft.uri}
                  className="overflow-hidden rounded-lg border border-border bg-background"
                >
                  <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted font-mono text-[10px] font-bold text-muted-foreground">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      <div className="min-w-0">
                        <div className="truncate text-xs font-semibold">{draft.name}</div>
                        <div className="truncate font-mono text-[10px] text-muted-foreground">
                          {draft.uri}
                        </div>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Pill tone="blue">{SCOPE_LABELS[draft.scope]}</Pill>
                      <span className="text-[10px] font-semibold text-[#1a7f37]">
                        +{stats.added}
                      </span>
                      <span className="text-[10px] font-semibold text-[#cf222e]">
                        -{stats.removed}
                      </span>
                    </div>
                  </header>
                  <div className="h-[280px]">
                    <DiffView original={draft.originalContent} next={draft.content} compact />
                  </div>
                </section>
              );
            })}
          </div>
          <DialogFooter className="mx-0 mb-0 shrink-0 px-5 py-3">
            <Button
              variant="destructive"
              disabled={saving}
              onClick={() => void discardEditing()}
            >
              <Trash2 className="size-4" />
              放弃全部
            </Button>
            <Button variant="outline" disabled={saving} onClick={() => setReviewOpen(false)}>
              返回编辑
            </Button>
            <Button disabled={saving || !draftList.length} onClick={() => void saveDrafts()}>
              {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              {saving ? "正在保存" : "确认保存 " + draftList.length + " 个文件"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <SkillTransferModal direction={transfer} onClose={() => setTransfer(null)}
        onImported={() => void refreshTree()} />
    </div>
  );
}
