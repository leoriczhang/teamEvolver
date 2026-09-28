import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  File,
  Folder,
  FolderInput,
  Pencil,
  RefreshCw,
  Save,
  Trash2,
  Upload,
} from "lucide-react";

import { api } from "@/api/client";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

interface SkillSource {
  source_id?: string;
  account?: string;
  path: string;
  name?: string;
  document_count: number;
  total_bytes: number;
  ready: boolean;
  status?: "ready" | "empty" | "missing" | "unavailable" | string;
  error?: string;
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

function relativeDepth(uri: string, root: string) {
  const relative = uri.startsWith(`${root}/`) ? uri.slice(root.length + 1) : uri;
  return Math.max(0, relative.split("/").length - 1);
}

export default function SkillSourceWorkspace({
  sources,
  selectedPath,
  onSelect,
  onRefresh,
  onUseForMining,
}: {
  sources: SkillSource[];
  selectedPath: string;
  onSelect: (path: string) => void;
  onRefresh: () => Promise<unknown>;
  onUseForMining: (path: string) => void;
}) {
  const [config, setConfig] = useState<SourceConfig | null>(null);
  const [activeSourceId, setActiveSourceId] = useState("");
  const [tree, setTree] = useState<TreeResponse | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<TreeEntry | null>(null);
  const [selectedDirectory, setSelectedDirectory] = useState("");
  const [content, setContent] = useState<SourceContent | null>(null);
  const [draft, setDraft] = useState("");
  const [original, setOriginal] = useState("");
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerAccount, setPickerAccount] = useState("");
  const [pickerTree, setPickerTree] = useState<TreeResponse | null>(null);
  const [pickerUri, setPickerUri] = useState("");
  const [pickerLoading, setPickerLoading] = useState(false);
  const [replaceSourceId, setReplaceSourceId] = useState("");
  const fileInput = useRef<HTMLInputElement | null>(null);
  const folderInput = useRef<HTMLInputElement | null>(null);

  const activeSource = sources.find((source) => source.source_id === activeSourceId)
    || sources.find((source) => source.path === selectedPath)
    || sources[0];
  const dirty = Boolean(content && draft !== original);
  const rows = useMemo(() => tree?.entries || [], [tree]);

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
      return;
    }
    setActiveSourceId(activeSource.source_id);
    void loadTree(activeSource.source_id, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSource?.source_id, activeSource?.status]);

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
      setEditing(false);
      return;
    }
    setLoading(true);
    try {
      const next = await api<SourceContent>(
        `/api/mining/ov-source-content?source_id=${encodeURIComponent(activeSource.source_id)}&uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(next);
      setDraft(next.content);
      setOriginal(next.content);
      setEditing(false);
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
          original_content: original,
        }),
      });
      setOriginal(result.content);
      setDraft(result.content);
      setContent({ ...content, content: result.content });
      setEditing(false);
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
    if (!source.source_id) return;
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
    setReplaceSourceId(sourceId);
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
      setPickerUri("");
    } catch (error: any) {
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setPickerLoading(false);
    }
  }

  async function reloadPicker(account: string) {
    setPickerAccount(account);
    if (!account) return;
    setPickerLoading(true);
    try {
      const next = await api<TreeResponse>(
        `/api/mining/ov-tree?account=${encodeURIComponent(account)}&uri=${encodeURIComponent(config?.root_uri || "viking://resources")}`,
      );
      setPickerTree(next);
      setPickerUri("");
    } catch (error: any) {
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setPickerLoading(false);
    }
  }

  async function registerSource() {
    if (!pickerAccount || !pickerUri) return;
    try {
      const result = await api<{ source: SkillSource }>("/api/mining/ov-sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account: pickerAccount, uri: pickerUri }),
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

  return (
    <div className="space-y-[18px] px-[22px] py-[22px]">
      <Panel
        title="OpenViking 知识源"
        count={`${sources.length} 个目录引用`}
        extra={(
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => void onRefresh()}><RefreshCw className="size-3.5" /> 刷新</Button>
            <Button size="sm" onClick={() => void openPicker()}><FolderInput className="size-3.5" /> 选择 OV 文件夹</Button>
          </div>
        )}
      >
        {sources.length === 0 ? (
          <div className="grid min-h-44 place-items-center px-6 text-center text-sm text-muted-foreground">
            <div><Folder className="mx-auto mb-3 size-8 text-muted-soft" />选择 OpenViking Resources 中的一个现有文件夹，将其登记为 Skill 挖掘知识源。</div>
          </div>
        ) : (
          <div className="grid min-h-[620px] lg:grid-cols-[270px_320px_minmax(0,1fr)]">
            <aside className="border-b border-border bg-surface-subtle p-2 lg:border-b-0 lg:border-r">
              {sources.map((source) => (
                <button
                  key={source.source_id || source.path}
                  type="button"
                  className={cn("mb-1 w-full rounded-lg border px-3 py-3 text-left", activeSource?.path === source.path ? "border-accent bg-accent-soft" : "border-transparent hover:bg-background")}
                  onClick={() => {
                    setActiveSourceId(source.source_id || "");
                    onSelect(source.path);
                  }}
                >
                  <div className="flex items-center gap-2"><Folder className="size-4 shrink-0 text-accent" /><span className="min-w-0 flex-1 truncate text-xs font-semibold">{source.name || source.path.split("/").pop()}</span><Pill tone={source.status === "missing" ? "red" : source.ready ? "green" : "amber"}>{source.status === "missing" ? "路径已删除" : source.status === "unavailable" ? "暂不可用" : source.ready ? "可用" : "空目录"}</Pill></div>
                  <div className="mono mt-2 break-all text-[10px] leading-4 text-muted-foreground">{source.path}</div>
                  <div className="mt-2 flex items-center justify-between text-[10.5px] text-muted-foreground"><span>{source.document_count} 个文件 · {formatBytes(source.total_bytes)}</span><span>{source.account}</span></div>
                </button>
              ))}
              {activeSource && (
                <div className="mt-3 space-y-2 border-t border-border pt-3">
                  {activeSource.status === "missing" && <Button size="sm" className="w-full" onClick={() => void openPicker(activeSource.source_id)}><FolderInput className="size-3.5" /> 重新选择路径</Button>}
                  <Button size="sm" variant="outline" className="w-full" onClick={() => onUseForMining(activeSource.path)} disabled={!activeSource.ready}>用于 Skill 挖掘</Button>
                  <Button size="sm" variant="outline" className="w-full text-destructive hover:text-destructive" onClick={() => void removeSource(activeSource)}><Trash2 className="size-3.5" /> 移除目录引用</Button>
                </div>
              )}
            </aside>

            <section className="flex min-h-0 flex-col border-b border-border lg:border-b-0 lg:border-r">
              <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
                <input ref={fileInput} type="file" multiple className="hidden" accept=".md,.markdown,.txt,.docx,.xlsx,.pptx,.pdf,.csv" onChange={(event) => void uploadFiles(event.target.files)} />
                <input ref={(node) => { folderInput.current = node; if (node) node.setAttribute("webkitdirectory", ""); }} type="file" multiple className="hidden" onChange={(event) => void uploadFiles(event.target.files)} />
                <Button size="sm" variant="outline" disabled={!activeSource || ["missing", "unavailable"].includes(activeSource.status || "") || uploading} onClick={() => fileInput.current?.click()}><Upload className="size-3.5" /> 上传文件</Button>
                <Button size="sm" variant="outline" disabled={!activeSource || ["missing", "unavailable"].includes(activeSource.status || "") || uploading} onClick={() => folderInput.current?.click()}><FolderInput className="size-3.5" /> 上传文件夹</Button>
                <Button size="sm" variant="outline" disabled={!selectedEntry} className="ml-auto text-destructive hover:text-destructive" onClick={() => void deleteEntry()}><Trash2 className="size-3.5" /></Button>
              </div>
              <div className="mono truncate border-b border-border px-3 py-2 text-[10px] text-muted-foreground">上传位置：{selectedDirectory || activeSource?.path || "—"}</div>
              <div className="min-h-0 flex-1 overflow-auto p-2">
                {activeSource?.status === "unavailable" ? <div className="p-4 text-xs leading-5 text-amber-700">暂时无法连接 OpenViking 检查该目录。请稍后刷新；为避免使用不确定输入，新任务暂不可启动。</div> : loading && !tree ? <div className="p-4 text-xs text-muted-foreground">正在读取目录…</div> : !tree?.exists ? <div className="p-4 text-xs leading-5 text-destructive">该知识源路径已从 OpenViking 删除。已有任务仍保留创建时的私有快照；新任务已禁止启动。</div> : rows.length === 0 ? <div className="p-4 text-xs text-muted-foreground">目录为空，可在此上传文件。</div> : rows.map((entry) => {
                  const depth = relativeDepth(entry.uri, tree.root_uri);
                  return <button key={entry.uri} type="button" onClick={() => void openEntry(entry)} title={entry.uri} className={cn("mb-0.5 flex min-h-8 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent-soft", selectedEntry?.uri === entry.uri && "bg-accent-soft text-accent")} style={{ paddingLeft: `${8 + depth * 14}px` }}>{entry.is_dir ? <><ChevronRight className="size-3" /><Folder className="size-3.5 text-accent" /></> : <><span className="w-3" /><File className="size-3.5 text-slate-500" /></>}<span className="min-w-0 flex-1 truncate">{entry.name}</span>{!entry.is_dir && <span className="text-[10px] text-muted-soft">{formatBytes(entry.size)}</span>}</button>;
                })}
              </div>
            </section>

            <section className="flex min-h-0 flex-col bg-background">
              {!content ? <div className="grid flex-1 place-items-center px-6 text-center text-sm text-muted-foreground">从文件树选择 Markdown 文件后，可在这里查看和编辑。</div> : <>
                <div className="flex items-center gap-2 border-b border-border px-4 py-3"><span className="mono min-w-0 flex-1 truncate text-xs font-semibold">{content.name}</span>{content.editable && !editing && <Button size="sm" variant="outline" onClick={() => setEditing(true)}><Pencil className="size-3.5" /> 编辑</Button>}{editing && <Button size="sm" disabled={saving || !dirty} onClick={() => void saveContent()}><Save className="size-3.5" /> {saving ? "保存中…" : "保存"}</Button>}</div>
                {editing ? <Textarea value={draft} onChange={(event) => setDraft(event.target.value)} className="mono min-h-0 flex-1 resize-none rounded-none border-0 p-5 text-xs focus-visible:ring-0" /> : <div className="min-h-0 flex-1 overflow-auto p-5">{/\.md$/i.test(content.name) ? <MarkdownDocument content={draft} /> : <pre className="mono whitespace-pre-wrap break-words text-xs leading-6">{draft}</pre>}</div>}
              </>}
            </section>
          </div>
        )}
      </Panel>

      <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
        <DialogContent className="flex h-[78vh] w-[calc(100vw-32px)] flex-col overflow-hidden p-0 !max-w-[820px]">
          <DialogHeader className="border-b border-border px-5 py-4"><DialogTitle>{replaceSourceId ? "重新选择知识源路径" : "选择 OpenViking 文件夹"}</DialogTitle></DialogHeader>
          <div className="flex items-center gap-3 border-b border-border p-4"><select value={pickerAccount} onChange={(event) => void reloadPicker(event.target.value)} className="rounded-lg border border-border bg-background px-3 py-2 text-xs">{config?.accounts.map((account) => <option key={account} value={account}>{account}</option>)}</select><Input value={pickerUri} readOnly placeholder="请从下方选择一个文件夹" className="mono flex-1 text-xs" /></div>
          <div className="min-h-0 flex-1 overflow-auto p-3">{pickerLoading ? <div className="p-4 text-xs text-muted-foreground">正在读取 OpenViking 目录…</div> : (pickerTree?.entries || []).filter((entry) => entry.is_dir).map((entry) => <button key={entry.uri} type="button" onClick={() => setPickerUri(entry.uri)} className={cn("mb-1 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs hover:bg-accent-soft", pickerUri === entry.uri && "bg-accent-soft text-accent")} style={{ paddingLeft: `${12 + relativeDepth(entry.uri, pickerTree?.root_uri || "viking://resources") * 16}px` }}><ChevronDown className="size-3 text-muted-soft" /><Folder className="size-4 text-accent" /><span className="min-w-0 flex-1 truncate">{entry.name}</span><span className="mono hidden max-w-[55%] truncate text-[10px] text-muted-soft sm:block">{entry.uri}</span></button>)}</div>
          <div className="flex justify-end gap-2 border-t border-border p-4"><Button variant="outline" onClick={() => setPickerOpen(false)}>取消</Button><Button disabled={!pickerUri} onClick={() => void registerSource()}>确认设为知识源</Button></div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
