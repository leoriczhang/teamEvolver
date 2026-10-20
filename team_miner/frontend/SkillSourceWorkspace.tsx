import { useEffect, useRef, useState } from "react";
import {
  File,
  Folder,
  FolderInput,
  RefreshCw,
  Search,
  Trash2,
  Upload,
} from "lucide-react";

import { api } from "@/api/client";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

import {
  KnowledgeFileEditorToolbar,
  KnowledgeVersionPanel,
  type KnowledgeEditorMode,
} from "./KnowledgeFileEditorControls";
import { KnowledgeWorkspaceTree } from "./KnowledgeWorkspaceTree";

interface SkillSource {
  source_id?: string;
  account?: string;
  path: string;
  name?: string;
  document_count: number;
  total_bytes: number;
  ready: boolean;
  status?: "ready" | "empty" | "missing" | "unavailable" | "over_limit" | string;
  error?: string;
  ingestion?: { error?: string };
  tags?: Array<"skill" | "knowledge" | string>;
  default?: boolean;
}

const SOURCE_TAGS = [
  { value: "skill", label: "Skill 知识源" },
  { value: "knowledge", label: "知识库知识源" },
] as const;

function hasSourceTag(source: SkillSource | undefined, tag: "skill" | "knowledge") {
  return Boolean(source?.tags?.includes(tag));
}

interface TreeEntry {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | null;
  modified_at?: string;
}

interface TreeResponse {
  root_uri: string;
  exists: boolean;
  entries: TreeEntry[];
}

interface SourceConfig {
  accounts: string[];
  current: string;
  root_uri: string;
}

interface SourceContent {
  source_id: string;
  uri: string;
  name: string;
  content: string;
  editable: boolean;
}

function formatBytes(value?: number | null) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function SkillSourceWorkspace({
  sources,
  selectedPath,
  onSelect,
  onRefresh,
}: {
  sources: SkillSource[];
  selectedPath: string;
  onSelect: (path: string) => void;
  onRefresh: () => Promise<unknown>;
}) {
  const [config, setConfig] = useState<SourceConfig | null>(null);
  const [activeSourceId, setActiveSourceId] = useState("");
  const [tree, setTree] = useState<TreeResponse | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<TreeEntry | null>(null);
  const [selectedDirectory, setSelectedDirectory] = useState("");
  const [content, setContent] = useState<SourceContent | null>(null);
  const [draft, setDraft] = useState("");
  const [editorMode, setEditorMode] = useState<KnowledgeEditorMode>("preview");
  const [versionPanelOpen, setVersionPanelOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerAccount, setPickerAccount] = useState("");
  const [pickerTree, setPickerTree] = useState<TreeResponse | null>(null);
  const [pickerUri, setPickerUri] = useState("");
  const [pickerLoading, setPickerLoading] = useState(false);
  const [pickerError, setPickerError] = useState("");
  const [replaceSourceId, setReplaceSourceId] = useState("");
  const [pickerTags, setPickerTags] = useState<Array<"skill" | "knowledge">>(["skill"]);
  const [tagSaving, setTagSaving] = useState(false);
  const [treeQuery, setTreeQuery] = useState("");
  const [treeSearchUnlocked, setTreeSearchUnlocked] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const folderInput = useRef<HTMLInputElement | null>(null);

  const activeSource = sources.find((source) => source.source_id === activeSourceId)
    || sources.find((source) => source.path === selectedPath)
    || sources[0];
  const dirty = Boolean(content && draft !== content.content);
  const sourceActions = (
    <div className="flex items-center gap-1.5">
      <Button size="sm" variant="outline" onClick={() => void onRefresh()}>
        <RefreshCw className="size-3.5" /> 刷新
      </Button>
      <Button size="sm" onClick={() => void openPicker()}>
        <FolderInput className="size-3.5" /> 添加知识源
      </Button>
    </div>
  );

  useEffect(() => {
    void api<SourceConfig & { sources: SkillSource[] }>("/api/mining/ov-sources")
      .then((next) => {
        setConfig(next);
        setPickerAccount((previous) => previous || next.current || next.accounts[0] || "");
      })
      .catch((error) => toastErr("读取 OpenViking 配置失败", error.message));
  }, [sources]);

  useEffect(() => {
    if (!activeSource?.source_id || activeSource.status === "missing" || activeSource.status === "unavailable") {
      setTree(activeSource?.status === "missing" ? { root_uri: activeSource.path, exists: false, entries: [] } : null);
      setSelectedEntry(null);
      setContent(null);
      setVersionPanelOpen(false);
      return;
    }
    setActiveSourceId(activeSource.source_id);
    void loadTree(activeSource.source_id, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSource?.source_id, activeSource?.status]);

  useEffect(() => {
    if (!versionPanelOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setVersionPanelOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [versionPanelOpen]);

  async function loadTree(sourceId: string, notify = false) {
    setLoading(true);
    try {
      const next = await api<TreeResponse>(`/api/mining/ov-source-tree?source_id=${encodeURIComponent(sourceId)}`);
      setTree(next);
      setSelectedDirectory((previous) => previous && previous.startsWith(next.root_uri) ? previous : next.root_uri);
      if (notify) toastOk("目录已刷新", next.root_uri);
    } catch (error: any) {
      toastErr("读取知识源失败", error.message);
      await onRefresh();
    } finally {
      setLoading(false);
    }
  }

  async function openEntry(entry: TreeEntry) {
    if (!activeSource?.source_id) return;
    if (dirty && !window.confirm("当前文件有未保存修改，确定切换吗？")) return;
    setSelectedEntry(entry);
    if (entry.is_dir) {
      setSelectedDirectory(entry.uri);
      setContent(null);
      setVersionPanelOpen(false);
      return;
    }
    setLoading(true);
    try {
      const next = await api<SourceContent>(
        `/api/mining/ov-source-content?source_id=${encodeURIComponent(activeSource.source_id)}&uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(next);
      setDraft(next.content);
      setEditorMode(/\.md$/i.test(next.name) ? "preview" : "edit");
      setVersionPanelOpen(false);
    } catch (error: any) {
      toastErr("读取文件失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  async function saveContent() {
    if (!activeSource?.source_id || !content) return;
    setSaving(true);
    try {
      const result = await api<{ content: string }>("/api/mining/ov-source-content", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_id: activeSource.source_id,
          uri: content.uri,
          content: draft,
          original_content: content.content,
        }),
      });
      setDraft(result.content);
      setContent({ ...content, content: result.content });
      toastOk("文件已保存", content.name);
    } catch (error: any) {
      toastErr("保存文件失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function uploadFiles(list: FileList | null) {
    if (!list?.length || !activeSource?.source_id) return;
    const selected = Array.from(list);
    setUploading(true);
    try {
      const files = await Promise.all(selected.map(async (file) => ({
        name: file.name,
        relative_path: file.webkitRelativePath || file.name,
        content_b64: await fileToB64(file),
      })));
      const result = await api<{ file_count: number }>("/api/mining/ov-source-upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_id: activeSource.source_id,
          parent_uri: selectedDirectory || activeSource.path,
          files,
        }),
      });
      toastOk(`已上传 ${result.file_count} 个文件`, "文件已统一转换为 Markdown");
      await Promise.all([loadTree(activeSource.source_id), onRefresh()]);
    } catch (error: any) {
      toastErr("上传知识文件失败", error.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
      if (folderInput.current) folderInput.current.value = "";
    }
  }

  async function deleteEntry() {
    if (!activeSource?.source_id || !selectedEntry || selectedEntry.uri === activeSource.path) return;
    if (!window.confirm(`确定删除 ${selectedEntry.name}？该操作会删除 OpenViking 中的对应内容。`)) return;
    try {
      await api("/api/mining/ov-entry", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_id: activeSource.source_id, uri: selectedEntry.uri }),
      });
      setSelectedEntry(null);
      setContent(null);
      toastOk("条目已删除", selectedEntry.name);
      await Promise.all([loadTree(activeSource.source_id), onRefresh()]);
    } catch (error: any) {
      toastErr("删除条目失败", error.message);
    }
  }

  async function removeSource(source: SkillSource) {
    if (!source.source_id || source.default) return;
    if (!window.confirm(`移除知识源“${source.name || source.path}”？OpenViking 原目录不会被删除。`)) return;
    try {
      await api(`/api/mining/ov-sources/${encodeURIComponent(source.source_id)}`, { method: "DELETE" });
      if (activeSourceId === source.source_id) setActiveSourceId("");
      toastOk("知识源引用已移除", "OpenViking 原目录仍然保留");
      await onRefresh();
    } catch (error: any) {
      toastErr("移除知识源失败", error.message);
    }
  }

  async function openPicker(sourceId = "") {
    setPickerUri("");
    setPickerTree(null);
    setPickerError("");
    setReplaceSourceId(sourceId);
    const replaced = sources.find((source) => source.source_id === sourceId);
    setPickerTags(
      replaced?.tags?.filter((tag): tag is "skill" | "knowledge" => tag === "skill" || tag === "knowledge")
        || ["skill"],
    );
    setPickerOpen(true);
    const account = pickerAccount || config?.current || config?.accounts[0] || "";
    if (!account) return;
    setPickerAccount(account);
    setPickerLoading(true);
    try {
      const next = await api<TreeResponse>(
        `/api/mining/ov-tree?account=${encodeURIComponent(account)}&uri=${encodeURIComponent(config?.root_uri || "viking://resources")}`,
      );
      setPickerTree(next);
    } catch (error: any) {
      setPickerError(error.message || "目录列表不可用");
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setPickerLoading(false);
    }
  }

  async function reloadPicker(account: string) {
    setPickerUri("");
    setPickerTree(null);
    setPickerError("");
    setPickerAccount(account);
    if (!account) return;
    setPickerLoading(true);
    try {
      const next = await api<TreeResponse>(
        `/api/mining/ov-tree?account=${encodeURIComponent(account)}&uri=${encodeURIComponent(config?.root_uri || "viking://resources")}`,
      );
      setPickerTree(next);
    } catch (error: any) {
      setPickerError(error.message || "目录列表不可用");
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setPickerLoading(false);
    }
  }

  async function registerSource() {
    if (!pickerAccount || !pickerUri || !pickerTags.length) return;
    try {
      const result = await api<{ source: SkillSource }>("/api/mining/ov-sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account: pickerAccount, uri: pickerUri, tags: pickerTags }),
      });
      if (replaceSourceId && replaceSourceId !== result.source.source_id) {
        await api(`/api/mining/ov-sources/${encodeURIComponent(replaceSourceId)}`, { method: "DELETE" });
      }
      setActiveSourceId(result.source.source_id || "");
      onSelect(result.source.path);
      setPickerOpen(false);
      toastOk(replaceSourceId ? "知识源路径已更新" : "知识源已登记", result.source.path);
      await onRefresh();
    } catch (error: any) {
      toastErr("登记知识源失败", error.message);
    }
  }

  async function toggleSourceTag(source: SkillSource, tag: "skill" | "knowledge") {
    if (!source.source_id || tagSaving || (source.default && tag === "knowledge")) return;
    const current = source.tags?.filter((item): item is "skill" | "knowledge" => item === "skill" || item === "knowledge") || [];
    const next = current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag];
    if (!next.length) {
      toastErr("无法移除用途标签", "知识源至少需要保留一种用途");
      return;
    }
    setTagSaving(true);
    try {
      await api(`/api/mining/ov-sources/${encodeURIComponent(source.source_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: next }),
      });
      toastOk("知识源用途已更新", next.map((item) => item === "skill" ? "Skill 知识源" : "知识库知识源").join("、"));
      await onRefresh();
    } catch (error: any) {
      toastErr("更新知识源用途失败", error.message);
    } finally {
      setTagSaving(false);
    }
  }

  return (
    <div className="space-y-3 px-[22px] py-[22px]">
      {sources.length === 0 ? (
        <Panel title="知识源" count="0 个目录引用" extra={sourceActions}>
          <div className="grid min-h-44 place-items-center px-6 text-center text-sm text-muted-foreground">
            <div><Folder className="mx-auto mb-3 size-8 text-muted-soft" />添加 OpenViking Resources 中的文件夹，并标记为 Skill 知识源、知识库知识源，或同时用于两种挖掘。</div>
          </div>
        </Panel>
      ) : (
        <div className="grid h-[calc(100vh-190px)] min-h-[620px] grid-cols-[190px_minmax(0,1fr)] gap-3 xl:grid-cols-[240px_minmax(0,1fr)]">
          <Panel className="flex min-h-0 flex-col" title="知识源" count={`${sources.length} 个目录引用`}>
            <nav className="min-h-0 flex-1 space-y-1 overflow-auto bg-surface-subtle p-2" aria-label="知识源导航">
              {sources.map((source) => {
                const active = activeSource?.path === source.path;
                return (
                  <div
                    key={source.source_id || source.path}
                    className={cn(
                      "rounded-[10px] border p-2.5 transition-colors",
                      active ? "border-foreground bg-foreground text-white shadow-sm" : "border-transparent bg-transparent text-foreground hover:border-border hover:bg-white",
                    )}
                  >
                    <button
                      type="button"
                      className="w-full text-left"
                      onClick={() => {
                        setActiveSourceId(source.source_id || "");
                        onSelect(source.path);
                      }}
                    >
                      <div className="flex items-center gap-2">
                        <Folder className={cn("size-4 shrink-0", active ? "text-emerald-300" : "text-accent")} />
                        <span className="min-w-0 flex-1 truncate text-xs font-semibold" title={source.name || source.path.split("/").pop()}>{source.name || source.path.split("/").pop()}</span>
                        <span title={source.ingestion?.error || source.error} className={cn("shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-bold", source.status === "missing" || source.status === "over_limit" ? "bg-red-100 text-red-700" : source.ready ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700")}>{source.status === "over_limit" ? "超过挖掘限额" : source.status === "missing" ? "路径已删除" : source.status === "unavailable" ? "暂不可用" : source.ready ? "可用" : "空目录"}</span>
                      </div>
                      <div title={source.path} className={cn("mono mt-1.5 line-clamp-2 break-all text-[9.5px] leading-4", active ? "text-white/60" : "text-muted-foreground")}>{source.path}</div>
                      <div className={cn("mt-1.5 flex items-center justify-between text-[10px]", active ? "text-white/60" : "text-muted-foreground")}><span>{source.document_count} 个文件 · {formatBytes(source.total_bytes)}</span><span>{source.account}</span></div>
                    </button>

                    <div className={cn("mt-2 flex flex-wrap gap-1 border-t pt-2", active ? "border-white/15" : "border-border")}>
                      {source.default && <span className={cn("rounded px-1.5 py-0.5 text-[9px] font-bold", active ? "bg-white/10 text-white/75" : "bg-muted text-muted-foreground")}>默认</span>}
                      {SOURCE_TAGS.map((tag) => {
                        const selected = hasSourceTag(source, tag.value);
                        const locked = Boolean(source.default && tag.value === "knowledge");
                        return (
                          <button
                            key={tag.value}
                            type="button"
                            disabled={tagSaving || locked}
                            aria-pressed={selected}
                            title={locked ? "默认知识库知识源必须保留知识库标签" : `切换${tag.label}`}
                            onClick={() => void toggleSourceTag(source, tag.value)}
                            className={cn(
                              "rounded border px-1.5 py-0.5 text-[9px] font-semibold",
                              active
                                ? selected ? "border-white/25 bg-white/15 text-white" : "border-white/15 text-white/55"
                                : selected ? "border-accent/45 bg-accent-soft text-accent" : "border-border text-muted-foreground",
                              (tagSaving || locked) && "cursor-not-allowed opacity-70",
                            )}
                          >
                            {tag.label}{locked ? " · 默认" : ""}
                          </button>
                        );
                      })}
                    </div>

                    <div className="mt-2 flex justify-end gap-1.5">
                      {source.status === "missing" && !source.default && (
                        <button type="button" className={cn("rounded-md border px-2 py-1 text-[10px] font-semibold", active ? "border-white/20 text-white/80 hover:bg-white/10" : "border-border text-muted-foreground hover:bg-white")} onClick={() => void openPicker(source.source_id)}><FolderInput className="mr-1 inline size-3" />重选</button>
                      )}
                      <button
                        type="button"
                        className={cn("rounded-md border px-2 py-1 text-[10px] font-semibold", active ? "border-white/20 text-red-200 hover:bg-white/10" : "border-border text-destructive hover:bg-white", source.default && "cursor-not-allowed opacity-45")}
                        disabled={source.default}
                        title={source.default ? "默认来源不可移除" : `删除知识源 ${source.name || source.path}`}
                        aria-label={source.default ? "默认来源不可移除" : `删除知识源 ${source.name || source.path}`}
                        onClick={() => void removeSource(source)}
                      >
                        <Trash2 className="mr-1 inline size-3" />删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </nav>
          </Panel>

          <Panel
            className="!mt-0 flex min-h-0 flex-col"
            title={<span className="flex min-w-0 items-center gap-2"><FolderInput className="size-4 text-accent" /><span className="truncate" title={activeSource?.name || "知识源文件"}>{activeSource?.name || "知识源文件"}</span></span>}
            count={tree?.exists ? `${tree.entries.length} 项` : undefined}
            extra={sourceActions}
          >
            <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(220px,0.4fr)_minmax(0,1fr)] lg:grid-cols-[minmax(230px,0.36fr)_minmax(0,1fr)] lg:grid-rows-1 xl:grid-cols-[minmax(270px,0.38fr)_minmax(0,1fr)]">
              <section className="flex min-h-0 flex-col border-b border-line lg:border-b-0 lg:border-r">
                <div className="flex flex-wrap items-center gap-1.5 border-b border-line bg-background/60 p-2">
                  <input ref={fileInput} type="file" multiple className="hidden" accept=".md,.markdown,.txt,.docx,.xlsx,.pptx,.pdf,.csv" onChange={(event) => void uploadFiles(event.target.files)} />
                  <input ref={(node) => { folderInput.current = node; if (node) node.setAttribute("webkitdirectory", ""); }} type="file" multiple className="hidden" onChange={(event) => void uploadFiles(event.target.files)} />
                  <Button size="sm" variant="outline" title="上传文件" aria-label="上传文件" disabled={!activeSource || ["missing", "unavailable"].includes(activeSource.status || "") || uploading} onClick={() => fileInput.current?.click()}><Upload className="size-3.5" /> <span className="hidden lg:inline">文件</span></Button>
                  <Button size="sm" variant="outline" title="上传文件夹" aria-label="上传文件夹" disabled={!activeSource || ["missing", "unavailable"].includes(activeSource.status || "") || uploading} onClick={() => folderInput.current?.click()}><FolderInput className="size-3.5" /> <span className="hidden lg:inline">文件夹</span></Button>
                  <Button size="sm" variant="outline" disabled={!selectedEntry} aria-label={selectedEntry ? `删除 ${selectedEntry.name}` : "删除条目"} title={selectedEntry ? `删除 ${selectedEntry.name}` : "请先选择条目"} onClick={() => void deleteEntry()}><Trash2 className="size-3.5" /> <span className="hidden lg:inline">删除</span></Button>
                </div>
                <div className="border-b border-line bg-background/60 px-3 py-2">
                  <div className="flex items-center gap-2"><span className="shrink-0 text-[10px] font-bold text-muted-foreground">知识源</span><code title={activeSource?.path || ""} className="block min-w-0 flex-1 truncate text-[10px] text-foreground">{activeSource?.path || "—"}</code></div>
                </div>
                <div className="border-b border-line p-2">
                  <div className="flex h-8 items-center gap-2 rounded-lg border border-input bg-white px-2.5">
                    <Search className="size-3.5 text-muted-soft" />
                    <input
                      type="search"
                      name="skill-source-file-search"
                      autoComplete="one-time-code"
                      data-lpignore="true"
                      readOnly={!treeSearchUnlocked}
                      onFocus={() => setTreeSearchUnlocked(true)}
                      value={treeQuery}
                      onChange={(event) => setTreeQuery(event.target.value)}
                      className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-soft"
                      placeholder="搜索文件…"
                    />
                  </div>
                </div>
                <div className="min-h-0 flex-1">
                  <KnowledgeWorkspaceTree
                    tree={activeSource?.status === "unavailable" ? { root_uri: activeSource.path, exists: false, entries: [] } : tree}
                    emptyText={activeSource?.status === "missing"
                      ? "该知识源路径已从 OpenViking 删除，已禁止用于挖掘。"
                      : activeSource?.status === "unavailable"
                        ? "暂时无法检查该知识源，已禁止用于挖掘。请稍后刷新。"
                        : activeSource?.status === "over_limit"
                          ? activeSource.ingestion?.error || activeSource.error || "单个文本文件不超过 20 MB，目录不超过 10000 个文件；大来源分批处理。"
                        : "该知识源为空，可上传文件或文件夹。"}
                    selectedUri={selectedEntry?.uri || content?.uri || ""}
                    selectedDirectoryUri={selectedDirectory}
                    query={treeQuery}
                    onSelect={(entry) => void openEntry(entry)}
                  />
                </div>
              </section>

              <section className="flex min-h-0 flex-col">
                <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 border-b border-line bg-surface-subtle px-[14px] py-2">
                  <span className="flex min-w-0 items-center gap-2 text-[13px] font-[780]"><File className="size-4 shrink-0 text-accent" /><span className="truncate" title={content?.name || "文件内容"}>{content?.name || "文件内容"}</span></span>
                  {content && (
                    <KnowledgeFileEditorToolbar
                      isMarkdown={/\.md$/i.test(content.name)}
                      mode={editorMode}
                      onModeChange={setEditorMode}
                      onVersion={() => setVersionPanelOpen(true)}
                      editable={content.editable}
                      dirty={dirty}
                      saving={saving}
                      onSave={() => void saveContent()}
                    />
                  )}
                </div>
                {content && <div className="flex items-center gap-2 border-b border-line bg-background/60 px-3 py-2"><code title={content.uri} className="block min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{content.uri}</code>{dirty && <span className="shrink-0 text-[10px] font-semibold text-accent">未保存</span>}</div>}
                <div className="min-h-0 flex-1">
                  {!content ? (
                    <div className="grid h-full place-items-center px-6 text-center text-xs leading-6 text-muted-foreground"><div><File className="mx-auto mb-2 size-8 text-muted-soft" />从左侧目录选择文件</div></div>
                  ) : /\.md$/i.test(content.name) && editorMode === "preview" ? (
                    <div className="h-full overflow-auto bg-white px-7 py-6"><MarkdownDocument content={draft} /></div>
                  ) : content.editable ? (
                    <textarea value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} className="h-full w-full resize-none border-0 bg-[#fbfcfe] p-5 font-mono text-xs leading-6 outline-none" aria-label={`${content.name} 文件编辑器`} />
                  ) : (
                    <pre className="h-full overflow-auto whitespace-pre-wrap break-words bg-[#fbfcfe] p-5 font-mono text-xs leading-6">{draft}</pre>
                  )}
                </div>
                {content && <div className="flex h-8 items-center border-t border-line bg-surface-subtle px-3 text-[10px] text-muted-foreground"><span>{content.editable ? "可编辑" : "当前类型只读"}</span><span className="ml-auto">{draft.split("\n").length} 行 · {draft.length.toLocaleString()} 字符</span></div>}
              </section>
            </div>
          </Panel>
        </div>
      )}

      <KnowledgeVersionPanel
        open={versionPanelOpen && Boolean(content)}
        name={content?.name || ""}
        uri={content?.uri || ""}
        onClose={() => setVersionPanelOpen(false)}
      />

      <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
        <DialogContent className="flex h-[78vh] w-[calc(100vw-32px)] flex-col overflow-hidden p-0 !max-w-[820px]">
          <DialogHeader className="border-b border-border px-5 py-4"><DialogTitle>{replaceSourceId ? "重新选择知识源路径" : "添加知识源"}</DialogTitle></DialogHeader>
          <div className="space-y-3 border-b border-border p-4">
            <div className="flex items-center gap-3"><select aria-label="知识源账号" value={pickerAccount} onChange={(event) => void reloadPicker(event.target.value)} className="rounded-lg border border-border bg-background px-3 py-2 text-xs">{config?.accounts.map((account) => <option key={account} value={account}>{account}</option>)}</select><Input aria-label="知识源 URI" value={pickerUri} title={pickerUri} onChange={(event) => setPickerUri(event.target.value)} placeholder="填写文件夹 URI，或从下方列表选择" className="mono flex-1 text-xs" /></div>
            <fieldset>
              <legend className="mb-2 text-[11px] font-bold text-muted-foreground">知识源用途（可多选）</legend>
              <div className="flex flex-wrap gap-2">
                {SOURCE_TAGS.map((tag) => (
                  <label key={tag.value} className={cn("flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold", pickerTags.includes(tag.value) ? "border-accent bg-accent-soft text-accent" : "border-border bg-background text-muted-foreground")}>
                    <input
                      type="checkbox"
                      checked={pickerTags.includes(tag.value)}
                      onChange={(event) => setPickerTags((previous) => event.target.checked ? [...previous, tag.value] : previous.filter((item) => item !== tag.value))}
                    />
                    {tag.label}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
          <div className="min-h-0 flex-1">
            {pickerLoading ? (
              <div className="p-4 text-xs text-muted-foreground">正在读取 OpenViking 目录…</div>
            ) : pickerTree ? (
              <KnowledgeWorkspaceTree
                tree={{ ...pickerTree, entries: pickerTree.entries.filter((entry) => entry.is_dir) }}
                emptyText="OpenViking Resources 中暂无可选择的文件夹。"
                selectedUri={pickerUri}
                selectedDirectoryUri={pickerUri}
                query=""
                onSelect={(entry) => setPickerUri(entry.uri)}
              />
            ) : (
              <div role="status" className="grid h-full min-h-40 place-items-center px-6 text-center text-xs leading-5 text-muted-foreground">
                {pickerError
                  ? "目录列表读取失败，可直接填写已知且有权访问的文件夹 URI。登记时会校验该目录。"
                  : "可直接填写已知且有权访问的文件夹 URI，或选择账号读取目录列表。"}
              </div>
            )}
          </div>
          <div className="flex justify-end gap-2 border-t border-border p-4"><Button variant="outline" onClick={() => setPickerOpen(false)}>取消</Button><Button disabled={!pickerAccount || !pickerUri || !pickerTags.length} onClick={() => void registerSource()}>确认设为知识源</Button></div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
