import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  BookOpenText,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Eye,
  File,
  Folder,
  FolderInput,
  GitCompareArrows,
  History,
  Link2,
  LoaderCircle,
  Network,
  PencilLine,
  Play,
  RefreshCw,
  Save,
  Search,
  Server,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, ApiError, tenantHeaders } from "@/api/client";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { MiningConfig, TreeEntry, TreeResponse, CompileCapabilities, CompileProgress, MiningTask, KnowledgeContent, KnowledgeLink, KnowledgeBacklink, WikiGraphResponse, WikiRoot, FileVisit, FileNavigation, UploadProgressState, TreeNode, WorkspaceTab, EditorMode, WikiViewMode, ACTIVE_TASK_STATES, TERMINAL_TASK_STATES, KNOWLEDGE_UPLOAD_ACCEPT, uploadJson, commonVikingRoot, knowledgeWorkspaceRoot, resolveKnowledgeHref, formatBytes, taskTone, taskLabel, formatCompileTime, buildTree, filterTree, TreeList, taskLogLines } from "./KnowledgeBaseMiningViewShared";

export function useKnowledgeBaseMiningView({ active }: { active: boolean }) {
const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("source");
const [config, setConfig] = useState<MiningConfig | null>(null);
const [account, setAccount] = useState("");
const [wikiRoot, setWikiRoot] = useState("");
const [wikiRoots, setWikiRoots] = useState<WikiRoot[]>([]);
const [sourceTree, setSourceTree] = useState<TreeResponse | null>(null);
const [wikiTree, setWikiTree] = useState<TreeResponse | null>(null);
const [capabilities, setCapabilities] = useState<CompileCapabilities | null>(null);
const [tasks, setTasks] = useState<MiningTask[]>([]);
const [selectedTaskId, setSelectedTaskId] = useState("");
const [selectedTask, setSelectedTask] = useState<MiningTask | null>(null);
const [instruction, setInstruction] = useState("按主题重组来源内容，生成可检索、可导航且保留来源引用的中文 Wiki；增量刷新已有页面。 ");
const [skillUri, setSkillUri] = useState("");
const [loading, setLoading] = useState(false);
const [starting, setStarting] = useState(false);
const [stopping, setStopping] = useState(false);
const [uploading, setUploading] = useState(false);
const [uploadProgress, setUploadProgress] = useState<UploadProgressState | null>(null);
const [deleting, setDeleting] = useState(false);
const [selectedTreeEntry, setSelectedTreeEntry] = useState<TreeEntry | null>(null);
const [selectedDirectoryUri, setSelectedDirectoryUri] = useState("");
const [treeQuery, setTreeQuery] = useState("");
const [selectedContent, setSelectedContent] = useState<KnowledgeContent | null>(null);
const [editorValue, setEditorValue] = useState("");
const [originalContent, setOriginalContent] = useState("");
const [editorMode, setEditorMode] = useState<EditorMode>("preview");
const [previewAnchor, setPreviewAnchor] = useState("");
const [loadingContent, setLoadingContent] = useState(false);
const [savingContent, setSavingContent] = useState(false);
const [versionPanelOpen, setVersionPanelOpen] = useState(false);
const [wikiViewMode, setWikiViewMode] = useState<WikiViewMode>("files");
const [wikiGraph, setWikiGraph] = useState<WikiGraphResponse | null>(null);
const [loadingWikiGraph, setLoadingWikiGraph] = useState(false);
const [wikiGraphError, setWikiGraphError] = useState("");
const [fileNavigation, setFileNavigation] = useState<FileNavigation>({ entries: [], index: -1 });
const [accountPanelOpen, setAccountPanelOpen] = useState(true);
const [backlinksOpen, setBacklinksOpen] = useState(true);
const fileInputRef = useRef<HTMLInputElement | null>(null);
const folderInputRef = useRef<HTMLInputElement | null>(null);
const logRef = useRef<HTMLPreElement | null>(null);
const loadConfig = useCallback(async () => {
    try {
      const next = await api<MiningConfig>("/api/knowledge-mining/config");
      setConfig(next);
      setSkillUri((previous) => previous || next.skill_uri);
      setWikiRoot((previous) => previous || next.wiki_root);
      setAccount((previous) => previous && next.accounts.includes(previous)
        ? previous
        : next.current || next.accounts[0] || "");
      if (next.error) toastErr("读取 OpenViking 账号失败", next.error);
    } catch (error: any) {
      toastErr("加载知识库挖掘配置失败", error.message);
    }
  }, []);
const loadWikiRoots = useCallback(async () => {
    if (!account || !config) return;
    try {
      const response = await api<{ default: string; roots: WikiRoot[] }>(
        `/api/knowledge-mining/wiki-roots?account=${encodeURIComponent(account)}`,
      );
      const roots = response.roots || [];
      setWikiRoots(roots);
      setWikiRoot((previous) => {
        if (previous && (previous === config.wiki_root || roots.some((item) => item.uri === previous))) {
          return previous;
        }
        return response.default || config.wiki_root || roots[0]?.uri || "";
      });
    } catch (error: any) {
      setWikiRoots([]);
      setWikiRoot(config.wiki_root);
      toastErr("读取历史知识库失败", error.message);
    }
  }, [account, config]);
const loadWikiGraph = useCallback(async () => {
    if (!account || !wikiRoot) return;
    setLoadingWikiGraph(true);
    setWikiGraphError("");
    try {
      const response = await api<WikiGraphResponse>("/api/knowledge-mining/graph", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account, wiki_root: wikiRoot }),
      });
      setWikiGraph(response);
    } catch (error: any) {
      setWikiGraph(null);
      setWikiGraphError(error.message);
    } finally {
      setLoadingWikiGraph(false);
    }
  }, [account, wikiRoot]);
const loadWorkspace = useCallback(async (notify = false) => {
    if (!account || !wikiRoot) return;
    setLoading(true);
    try {
      const [source, wiki, taskList, compileCapabilities] = await Promise.all([
        api<TreeResponse>(`/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=source`),
        api<TreeResponse>(`/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=wiki&wiki_root=${encodeURIComponent(wikiRoot)}`),
        api<{ tasks: MiningTask[] }>(`/api/knowledge-mining/tasks?account=${encodeURIComponent(account)}`),
        api<CompileCapabilities>(`/api/knowledge-mining/capabilities?account=${encodeURIComponent(account)}`),
      ]);
      setSourceTree(source);
      setWikiTree(wiki);
      setTasks(taskList.tasks || []);
      setCapabilities(compileCapabilities);
      setSelectedTaskId((previous) => previous || taskList.tasks?.[0]?.task_id || "");
      if (notify) toastOk("目录已刷新", account);
    } catch (error: any) {
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setLoading(false);
    }
  }, [account, wikiRoot]);
const loadTask = useCallback(async () => {
    if (!account || !selectedTaskId) {
      setSelectedTask(null);
      return;
    }
    try {
      const response = await api<{ task: MiningTask }>(
        `/api/knowledge-mining/tasks/${encodeURIComponent(selectedTaskId)}?account=${encodeURIComponent(account)}`,
      );
      setSelectedTask((previous) => {
        if (response.task.status === "completed" && previous?.status !== "completed") {
          setWikiGraph(null);
        }
        return response.task;
      });
      if (response.task.status === "completed") {
        const [wiki, compileCapabilities] = await Promise.all([
          api<TreeResponse>(
            `/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=wiki&wiki_root=${encodeURIComponent(wikiRoot)}`,
          ),
          api<CompileCapabilities>(`/api/knowledge-mining/capabilities?account=${encodeURIComponent(account)}`),
        ]);
        setWikiTree(wiki);
        setCapabilities(compileCapabilities);
      }
    } catch (error: any) {
      toastErr("读取挖掘任务失败", error.message);
    }
  }, [account, selectedTaskId, wikiRoot]);
useEffect(() => {
    if (!active) return;
    void loadConfig();
  }, [active, loadConfig]);
useEffect(() => {
    if (!active || !account) return;
    void loadWikiRoots();
  }, [active, account, loadWikiRoots]);
useEffect(() => {
    if (!active || !account || !wikiRoot) return;
    setSourceTree(null);
    setWikiTree(null);
    setTasks([]);
    setCapabilities(null);
    setSelectedTaskId("");
    setSelectedTask(null);
    setSelectedContent(null);
    setSelectedTreeEntry(null);
    setSelectedDirectoryUri("");
    setEditorValue("");
    setOriginalContent("");
    setPreviewAnchor("");
    setVersionPanelOpen(false);
    setWikiViewMode("files");
    setWikiGraph(null);
    setWikiGraphError("");
    setFileNavigation({ entries: [], index: -1 });
    void loadWorkspace(false);
  }, [active, account, loadWorkspace, wikiRoot]);
useEffect(() => {
    if (!active || workspaceTab !== "wiki" || wikiViewMode !== "graph" || wikiGraph || loadingWikiGraph) return;
    void loadWikiGraph();
  }, [active, loadWikiGraph, loadingWikiGraph, wikiGraph, wikiViewMode, workspaceTab]);
useEffect(() => {
    if (!active || !selectedTaskId) return;
    void loadTask();
    const timer = window.setInterval(() => void loadTask(), 2500);
    return () => window.clearInterval(timer);
  }, [active, selectedTaskId, loadTask]);
useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [selectedTask]);
const currentStatus = String(selectedTask?.status || "");
const running = ACTIVE_TASK_STATES.has(currentStatus);
const logs = useMemo(() => taskLogLines(selectedTask), [selectedTask]);
const contentDirty = Boolean(selectedContent && editorValue !== originalContent);
const selectedIsMarkdown = /\.md$/i.test(selectedContent?.name || "");
const canGoBack = fileNavigation.index > 0;
const canGoForward = fileNavigation.index >= 0 && fileNavigation.index < fileNavigation.entries.length - 1;
function switchWorkspaceTab(next: WorkspaceTab) {
    if (next === workspaceTab) return;
    if (contentDirty && !window.confirm("当前文件有未保存的修改，确定离开吗？")) return;
    setWorkspaceTab(next);
    setTreeQuery("");
    setSelectedTreeEntry(null);
    setSelectedDirectoryUri("");
    setSelectedContent(null);
    setEditorValue("");
    setOriginalContent("");
    setEditorMode("preview");
    setPreviewAnchor("");
    setVersionPanelOpen(false);
  }
async function openFile(
    kind: "source" | "wiki",
    entry: TreeEntry,
    options: {
      recordHistory?: boolean;
      historyIndex?: number;
      skipDirtyCheck?: boolean;
      wikiRoot?: string;
      activateWorkspace?: boolean;
      fragment?: string;
    } = {},
  ) {
    if (!account || entry.is_dir) return;
    if (
      !options.skipDirtyCheck
      && contentDirty
      && entry.uri !== selectedContent?.uri
      && !window.confirm("当前文件有未保存的修改，确定打开其他文件吗？")
    ) return;
    const requestWikiRoot = options.wikiRoot || wikiRoot;
    setVersionPanelOpen(false);
    setLoadingContent(true);
    try {
      const response = await api<KnowledgeContent>(
        `/api/knowledge-mining/content?account=${encodeURIComponent(account)}&kind=${kind}&uri=${encodeURIComponent(entry.uri)}&wiki_root=${encodeURIComponent(requestWikiRoot)}`,
      );
      if (options.activateWorkspace) {
        setWorkspaceTab(kind);
        setWikiViewMode("files");
        setTreeQuery("");
      }
      setSelectedContent(response);
      setEditorValue(response.content);
      setOriginalContent(response.content);
      setEditorMode(/\.md$/i.test(response.name) ? "preview" : "edit");
      setPreviewAnchor(/\.md$/i.test(response.name) ? options.fragment || "" : "");
      const visit: FileVisit = {
        kind,
        uri: response.uri,
        name: response.name,
        wiki_root: requestWikiRoot,
      };
      if (options.recordHistory === false && options.historyIndex != null) {
        setFileNavigation((previous) => ({ ...previous, index: options.historyIndex! }));
      } else {
        setFileNavigation((previous) => {
          const current = previous.entries[previous.index];
          if (
            current
            && current.kind === visit.kind
            && current.uri === visit.uri
            && current.wiki_root === visit.wiki_root
          ) return previous;
          const entries = [...previous.entries.slice(0, previous.index + 1), visit];
          return { entries, index: entries.length - 1 };
        });
      }
    } catch (error: any) {
      toastErr("打开文件失败", error.message);
    } finally {
      setLoadingContent(false);
    }
  }
async function navigateFileHistory(offset: -1 | 1) {
    const nextIndex = fileNavigation.index + offset;
    const visit = fileNavigation.entries[nextIndex];
    if (!visit || loadingContent) return;
    if (contentDirty && !window.confirm("当前文件有未保存的修改，确定离开吗？")) return;
    await openFile(
      visit.kind,
      { uri: visit.uri, name: visit.name, is_dir: false },
      {
        recordHistory: false,
        historyIndex: nextIndex,
        skipDirtyCheck: true,
        wikiRoot: visit.wiki_root,
        activateWorkspace: true,
      },
    );
  }
function handleMarkdownLink(href: string): boolean {
    if (!selectedContent || !config) return false;
    if (/^(?:https?:|mailto:|tel:)/i.test(href) || href.startsWith("#")) return false;
    const destination = resolveKnowledgeHref(
      href,
      selectedContent,
      config.source_root,
      wikiRoot,
      sourceTree,
      wikiTree,
    );
    if (destination) {
      void openFile(destination.kind, destination.entry, {
        activateWorkspace: true,
        fragment: destination.fragment,
      });
    } else {
      toastErr("无法打开知识链接", href);
    }
    return true;
  }
async function saveFile() {
    if (!selectedContent || !selectedContent.editable || !contentDirty) return;
    setSavingContent(true);
    try {
      const response = await api<{ content: string; link_index?: KnowledgeContent["link_index"] }>("/api/knowledge-mining/content", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account,
          kind: selectedContent.kind,
          uri: selectedContent.uri,
          wiki_root: wikiRoot,
          content: editorValue,
          original_content: originalContent,
        }),
      });
      setEditorValue(response.content);
      setOriginalContent(response.content);
      setSelectedContent((previous) => previous && previous.uri === selectedContent.uri
        ? { ...previous, content: response.content, link_index: response.link_index ?? previous.link_index }
        : previous);
      if (selectedContent.kind === "wiki") setWikiGraph(null);
      toastOk("文件已保存", selectedContent.name);
    } catch (error: any) {
      toastErr("保存文件失败", error.message);
    } finally {
      setSavingContent(false);
    }
  }
async function uploadFiles(files: File[]) {
    const parentUri = selectedDirectoryUri || activeRoot || "";
    if (!account || !parentUri || !files.length || (activeKind === "wiki" && !wikiRoot)) return;
    setUploading(true);
    const totalBytes = Math.max(1, files.reduce((total, file) => total + file.size, 0));
    let completedBytes = 0;
    setUploadProgress({
      percent: 0,
      label: "正在读取文件",
      currentFile: files[0]?.name || "",
      completedFiles: 0,
      totalFiles: files.length,
    });
    try {
      const payload: Array<{ name: string; relative_path: string; content_b64: string }> = [];
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index];
        const contentB64 = await fileToB64(file, (loaded) => {
          const percent = Math.min(35, Math.round(((completedBytes + loaded) / totalBytes) * 35));
          setUploadProgress({
            percent,
            label: "正在读取文件",
            currentFile: file.name,
            completedFiles: index,
            totalFiles: files.length,
          });
        });
        payload.push({
          name: file.name,
          relative_path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
          content_b64: contentB64,
        });
        completedBytes += file.size;
      }
      const response = await uploadJson<{ file_count: number; parent_uri: string }>(
        "/api/knowledge-mining/upload",
        { account, kind: activeKind, wiki_root: wikiRoot, parent_uri: parentUri, files: payload },
        (ratio) => setUploadProgress({
          percent: ratio >= 1 ? 85 : 35 + Math.round(ratio * 45),
          label: ratio >= 1 ? "OpenViking 正在转换并写入" : "正在上传",
          currentFile: ratio >= 1 ? "" : `${files.length} 个文件`,
          completedFiles: ratio >= 1 ? files.length : 0,
          totalFiles: files.length,
        }),
      );
      setUploadProgress({
        percent: 100,
        label: "上传完成",
        currentFile: "",
        completedFiles: response.file_count,
        totalFiles: response.file_count,
      });
      const targetLabel = activeKind === "source" ? "知识源" : "知识库";
      toastOk(`${targetLabel}已上传`, `${response.file_count} 个文件已保存到 ${response.parent_uri}`);
      window.setTimeout(() => void loadWorkspace(false), 1200);
      window.setTimeout(() => setUploadProgress(null), 1800);
    } catch (error: any) {
      toastErr(`上传${activeKind === "source" ? "知识源" : "知识库"}失败`, error.message);
      setUploadProgress(null);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (folderInputRef.current) folderInputRef.current.value = "";
    }
  }
async function deleteSelectedEntry() {
    if (!account || !selectedTreeEntry || deleting) return;
    const entry = selectedTreeEntry;
    const targetType = entry.is_dir ? "文件夹及其中全部内容" : "文件";
    if (!window.confirm(`确认删除${targetType}“${entry.name}”？此操作不可撤销。`)) return;
    setDeleting(true);
    try {
      await api<{ deleted: boolean }>("/api/knowledge-mining/entry", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account,
          kind: activeKind,
          wiki_root: wikiRoot,
          uri: entry.uri,
        }),
      });
      if (
        selectedContent
        && (selectedContent.uri === entry.uri || selectedContent.uri.startsWith(`${entry.uri}/`))
      ) {
        setSelectedContent(null);
        setEditorValue("");
        setOriginalContent("");
      }
      if (selectedDirectoryUri === entry.uri || selectedDirectoryUri.startsWith(`${entry.uri}/`)) {
        setSelectedDirectoryUri("");
      }
      setSelectedTreeEntry(null);
      if (activeKind === "wiki") setWikiGraph(null);
      await loadWorkspace(false);
      toastOk("已删除", entry.uri);
    } catch (error: any) {
      toastErr("删除失败", error.message);
    } finally {
      setDeleting(false);
    }
  }
async function startMining() {
    if (!account || !config) return;
    setStarting(true);
    try {
      const response = await api<{ task_id: string }>("/api/knowledge-mining/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account,
          source_uri: config.source_root,
          target_uri: config.wiki_root,
          skill_uri: skillUri,
          instruction: instruction.trim(),
        }),
      });
      setSelectedTaskId(response.task_id);
      toastOk("知识库挖掘已启动", `任务 ${response.task_id}`);
      await loadWorkspace(false);
    } catch (error: any) {
      toastErr("启动知识库挖掘失败", error.message);
    } finally {
      setStarting(false);
    }
  }
async function stopMining() {
    if (!account || !selectedTaskId) return;
    setStopping(true);
    try {
      const response = await api<{ task: MiningTask }>(
        `/api/knowledge-mining/tasks/${encodeURIComponent(selectedTaskId)}/cancel`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account }),
        },
      );
      setSelectedTask(response.task);
      toastOk("已请求停止任务", selectedTaskId);
    } catch (error: any) {
      toastErr("停止任务失败", error.message);
    } finally {
      setStopping(false);
    }
  }
useEffect(() => {
    function saveShortcut(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
      if (!selectedContent?.editable || !contentDirty || savingContent) return;
      event.preventDefault();
      void saveFile();
    }
    window.addEventListener("keydown", saveShortcut);
    return () => window.removeEventListener("keydown", saveShortcut);
  }, [contentDirty, editorValue, originalContent, savingContent, selectedContent]);
useEffect(() => {
    if (!versionPanelOpen) return;
    const previousOverflow = document.body.style.overflow;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setVersionPanelOpen(false);
    }
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [versionPanelOpen]);

const activeKind: "source" | "wiki" = workspaceTab === "wiki" ? "wiki" : "source";
const activeTree = activeKind === "source" ? sourceTree : wikiTree;
const activeRoot = activeKind === "source" ? config?.source_root : wikiRoot;
const activeUploadParent = selectedDirectoryUri || activeRoot || "";
return { workspaceTab, config, account, setAccount, wikiRoot, setWikiRoot, wikiRoots, sourceTree, capabilities, tasks, selectedTaskId, setSelectedTaskId, selectedTask, instruction, setInstruction, loading, starting, stopping, uploading, uploadProgress, deleting, selectedTreeEntry, setSelectedTreeEntry, selectedDirectoryUri, setSelectedDirectoryUri, treeQuery, setTreeQuery, selectedContent, editorValue, setEditorValue, editorMode, setEditorMode, previewAnchor, setPreviewAnchor, loadingContent, savingContent, versionPanelOpen, setVersionPanelOpen, wikiViewMode, setWikiViewMode, wikiGraph, loadingWikiGraph, wikiGraphError, accountPanelOpen, setAccountPanelOpen, backlinksOpen, setBacklinksOpen, fileInputRef, folderInputRef, logRef, loadWikiGraph, loadWorkspace, currentStatus, running, logs, contentDirty, selectedIsMarkdown, canGoBack, canGoForward, switchWorkspaceTab, openFile, navigateFileHistory, handleMarkdownLink, saveFile, uploadFiles, deleteSelectedEntry, startMining, stopMining, activeKind, activeTree, activeRoot, activeUploadParent };
}
