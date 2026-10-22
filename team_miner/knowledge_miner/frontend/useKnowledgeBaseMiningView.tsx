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
import {
  normalizeCompileCapabilities, normalizeKnowledgeContent, normalizeKnowledgeTree, normalizeMiningConfig,
  normalizeWikiGraph,
} from "./knowledgeMiningResponses";
import {
  cancelKnowledgeTask, loadKnowledgeTask, submitKnowledgeTask, waitForKnowledgeCommand, type KnowledgeMiningReceipt,
} from "./knowledgeMiningCommands";
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
const selectedContentRef = useRef(selectedContent);
selectedContentRef.current = selectedContent;
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
      const next = normalizeMiningConfig(await api("/api/knowledge-mining/config"));
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
      const roots = (response.roots || []).filter((item) => item.uri !== config.wiki_root);
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
      const request: RequestInit = {
        method: "POST",
        headers: tenantHeaders("/api/knowledge-mining/graph", { "Content-Type": "application/json" }),
        body: JSON.stringify({ account, wiki_root: wikiRoot }),
      };
      const response = await waitForKnowledgeCommand(await api("/api/knowledge-mining/graph", request), request);
      setWikiGraph(normalizeWikiGraph(response, account, wikiRoot));
    } catch (error: any) {
      setWikiGraph(null);
      setWikiGraphError(error.message);
    } finally {
      setLoadingWikiGraph(false);
    }
  }, [account, wikiRoot]);
const loadWorkspace = useCallback(async (notify = false) => {
    if (!account || !wikiRoot || !config) return;
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        (async () => setSourceTree(normalizeKnowledgeTree(await api(
          `/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=source`,
        ), "source", config.source_root, account)))(),
        (async () => setWikiTree(normalizeKnowledgeTree(await api(
          `/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=wiki&wiki_root=${encodeURIComponent(wikiRoot)}`,
        ), "wiki", wikiRoot, account)))(),
        (async () => {
          const taskList = await api<{ tasks: MiningTask[] }>(`/api/knowledge-mining/tasks?account=${encodeURIComponent(account)}`);
          setTasks(taskList.tasks || []);
          setSelectedTaskId((previous) => previous || taskList.tasks?.[0]?.task_id || "");
        })(),
        (async () => setCapabilities(normalizeCompileCapabilities(await api(
          `/api/knowledge-mining/capabilities?account=${encodeURIComponent(account)}`,
        ), account)))(),
      ]);
      const failures = results.filter((result) => result.status === "rejected");
      if (failures.length) {
        if (results[3].status === "rejected") {
          setCapabilities({ account, configured: null, can_create: false, reason_code: "能力检测失败" });
        }
        toastErr("部分工作区信息读取失败", failures.map((result) => String(result.reason?.message || result.reason)).join("；"));
      } else if (notify) toastOk("目录已刷新", account);
    } catch (error: any) {
      toastErr("读取 OpenViking 目录失败", error.message);
    } finally {
      setLoading(false);
    }
  }, [account, wikiRoot, config]);
const loadTask = useCallback(async () => {
    if (!account || !selectedTaskId) {
      setSelectedTask(null);
      return;
    }
    try {
      const task = await loadKnowledgeTask(selectedTaskId, account);
      if (task.task_id !== selectedTaskId) setSelectedTaskId(task.task_id);
      setSelectedTask((previous) => {
        if (task.status === "completed" && previous?.status !== "completed") {
          setWikiGraph(null);
        }
        return task;
      });
      if (task.status === "completed") {
        const [wiki, compileCapabilities] = await Promise.all([
          api<TreeResponse>(
            `/api/knowledge-mining/tree?account=${encodeURIComponent(account)}&kind=wiki&wiki_root=${encodeURIComponent(wikiRoot)}`,
          ),
          api<CompileCapabilities>(`/api/knowledge-mining/capabilities?account=${encodeURIComponent(account)}`),
        ]);
        setWikiTree(normalizeKnowledgeTree(wiki, "wiki", wikiRoot, account));
        setCapabilities(normalizeCompileCapabilities(compileCapabilities, account));
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
    if (!active || workspaceTab !== "wiki" || wikiViewMode !== "graph" || wikiGraph || loadingWikiGraph || wikiGraphError) return;
    void loadWikiGraph();
  }, [active, loadWikiGraph, loadingWikiGraph, wikiGraph, wikiGraphError, wikiViewMode, workspaceTab]);
const selectedTaskTerminal = selectedTask?.task_id === selectedTaskId
    && TERMINAL_TASK_STATES.has(String(selectedTask?.status));
useEffect(() => {
    if (!active || !selectedTaskId || selectedTaskTerminal) return;
    void loadTask();
    const timer = window.setInterval(() => void loadTask(), 2500);
    return () => window.clearInterval(timer);
  }, [active, selectedTaskId, selectedTaskTerminal, loadTask]);
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
      const response = normalizeKnowledgeContent(await api(
        `/api/knowledge-mining/content?account=${encodeURIComponent(account)}&kind=${kind}&uri=${encodeURIComponent(entry.uri)}&wiki_root=${encodeURIComponent(requestWikiRoot)}`,
      ), kind, account);
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
    const savedFile = selectedContent;
    const submittedContent = editorValue;
    setSavingContent(true);
    try {
      const request: RequestInit = {
        method: "POST",
        headers: tenantHeaders("/api/knowledge-mining/content", { "Content-Type": "application/json" }),
        body: JSON.stringify({
          account,
          kind: savedFile.kind,
          uri: savedFile.uri,
          wiki_root: wikiRoot,
          content: submittedContent,
          original_content: originalContent,
        }),
      };
      await waitForKnowledgeCommand(await api("/api/knowledge-mining/content", request), request);
      const response = normalizeKnowledgeContent(await api(
        `/api/knowledge-mining/content?account=${encodeURIComponent(account)}&kind=${savedFile.kind}&uri=${encodeURIComponent(savedFile.uri)}`,
        { headers: request.headers },
      ), savedFile.kind, account);
      const currentFile = selectedContentRef.current;
      if (currentFile?.uri === savedFile.uri && currentFile.kind === savedFile.kind) {
        setEditorValue((current) => current === submittedContent ? response.content : current);
        setOriginalContent(response.content);
        setSelectedContent((previous) => previous?.uri === savedFile.uri && previous.kind === savedFile.kind
          ? { ...previous, ...response, links: response.links ?? [], backlinks: response.backlinks ?? [] }
          : previous);
      }
      if (savedFile.kind === "wiki") setWikiGraph(null);
      toastOk("文件已保存", savedFile.name);
    } catch (error: any) {
      toastErr("保存文件失败", error.message);
    } finally {
      setSavingContent(false);
    }
  }
async function uploadFiles(files: File[]) {
    const parentUri = selectedDirectoryUri || activeRoot || "";
    if (!account || !parentUri || !files.length || (activeKind === "wiki" && !wikiRoot)) return;
    const request: RequestInit = {
      headers: tenantHeaders("/api/knowledge-mining/upload", { "Content-Type": "application/json" }),
    };
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
      const payload: Array<{ path: string; content_b64: string }> = [];
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
          path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
          content_b64: contentB64,
        });
        completedBytes += file.size;
      }
      const accepted = await uploadJson<KnowledgeMiningReceipt | { files?: unknown[]; file_count?: number }>(
        "/api/knowledge-mining/upload",
        { account, kind: activeKind, directory_uri: parentUri, files: payload },
        (ratio) => setUploadProgress({
          percent: ratio >= 1 ? 85 : 35 + Math.round(ratio * 45),
          label: ratio >= 1 ? "OpenViking 正在转换并写入" : "正在上传",
          currentFile: ratio >= 1 ? "" : `${files.length} 个文件`,
          completedFiles: ratio >= 1 ? files.length : 0,
          totalFiles: files.length,
        }),
        request.headers,
      );
      const response = await waitForKnowledgeCommand<{ files?: unknown[]; file_count?: number }>(accepted, request);
      const fileCount = response.files?.length ?? response.file_count;
      if (typeof fileCount !== "number") throw new Error("上传任务未返回已保存的文件列表");
      setUploadProgress({
        percent: 100,
        label: "上传完成",
        currentFile: "",
        completedFiles: fileCount,
        totalFiles: fileCount,
      });
      const targetLabel = activeKind === "source" ? "知识源" : "知识库";
      toastOk(`${targetLabel}已上传`, `${fileCount} 个文件已保存到 ${parentUri}`);
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
      const parameters = new URLSearchParams({ account, kind: activeKind, uri: entry.uri });
      const request: RequestInit = {
        method: "DELETE",
        headers: tenantHeaders("/api/knowledge-mining/entry"),
      };
      await waitForKnowledgeCommand(await api(`/api/knowledge-mining/entry?${parameters}`, request), request, false);
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
      const task = await submitKnowledgeTask({
          account,
          source_uri: config.source_root,
          target_uri: config.wiki_root,
          skill_uri: skillUri,
          instruction: instruction.trim(),
      });
      setSelectedTaskId(task.task_id);
      setSelectedTask(task);
      toastOk("知识库挖掘已受理", `任务 ${task.task_id}`);
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
      setSelectedTask(await cancelKnowledgeTask(selectedTaskId, account));
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
