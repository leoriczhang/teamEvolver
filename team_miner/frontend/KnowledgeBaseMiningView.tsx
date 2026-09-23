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

interface MiningConfig {
  accounts: string[];
  current: string;
  source_root: string;
  wiki_root: string;
  skill_uri: string;
  endpoint: string;
  account_source: string;
  error?: string;
}

interface TreeEntry {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | null;
  modified_at?: string;
  abstract?: string;
}

interface TreeResponse {
  account: string;
  kind: "source" | "wiki";
  root_uri: string;
  exists: boolean;
  entries: TreeEntry[];
}

interface CompileCapabilities {
  account: string;
  configured: boolean | null;
  can_create: boolean;
  probe_supported?: boolean;
  reason_code?: string | null;
  last_compile_time?: string | null;
  incremental?: boolean;
}

interface CompileProgress {
  percent: number;
  mode: "reported" | "stage";
  stage: string;
  label: string;
}

interface MiningTask {
  task_id: string;
  task_type?: string;
  status?: string;
  stage?: string;
  resource_id?: string;
  created_at?: string;
  updated_at?: string;
  result?: Record<string, any> | null;
  error?: Record<string, any> | string | null;
  events?: Array<Record<string, any>>;
  progress?: CompileProgress;
}

interface KnowledgeContent {
  account: string;
  kind: "source" | "wiki";
  uri: string;
  name: string;
  content: string;
  editable: boolean;
  links?: KnowledgeLink[];
  backlinks?: KnowledgeBacklink[];
  link_index?: {
    status: "ready" | "pending" | "error";
    generated_at?: string;
    page_count?: number;
    edge_count?: number;
    link_count?: number;
    message?: string;
  } | null;
}

interface KnowledgeLink {
  target_uri: string;
  target_name: string;
  target_path: string;
  labels: string[];
  lines: number[];
  count: number;
}

interface KnowledgeBacklink {
  source_uri: string;
  source_name: string;
  source_path: string;
  labels: string[];
  lines: number[];
  count: number;
}

interface WikiGraphResponse {
  account: string;
  wiki_root: string;
  source: "openviking-api";
  renderer: "local-html-fallback";
  page_count: number;
  edge_count: number;
  generated_at: string;
  html: string;
}

interface WikiRoot {
  uri: string;
  name: string;
  page_count: number;
  modified_at?: string;
  abstract?: string;
}

interface FileVisit {
  kind: "source" | "wiki";
  uri: string;
  name: string;
  wiki_root: string;
}

interface FileNavigation {
  entries: FileVisit[];
  index: number;
}

interface UploadProgressState {
  percent: number;
  label: string;
  currentFile: string;
  completedFiles: number;
  totalFiles: number;
}

interface TreeNode extends TreeEntry {
  children: TreeNode[];
}

type WorkspaceTab = "source" | "wiki" | "mining";
type EditorMode = "edit" | "preview";
type WikiViewMode = "files" | "graph";

const ACTIVE_TASK_STATES = new Set(["accepted", "pending", "queued", "running", "committing", "cancelling"]);
const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
const KNOWLEDGE_UPLOAD_ACCEPT = ".md,.markdown,.txt,.docx,.xlsx,.pptx,.pdf,.csv";

function uploadJson<T>(
  path: string,
  body: Record<string, unknown>,
  onProgress: (ratio: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    request.timeout = 15 * 60 * 1000;
    request.withCredentials = true;
    tenantHeaders(path, { "Content-Type": "application/json" }).forEach((value, key) => {
      request.setRequestHeader(key, value);
    });
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / Math.max(1, event.total));
    };
    request.upload.onload = () => onProgress(1);
    request.onerror = () => reject(new ApiError("上传连接中断，请检查网络后重试"));
    request.ontimeout = () => reject(new ApiError("上传处理超时，请刷新目录确认已写入的文件"));
    request.onload = () => {
      let response: any = {};
      try {
        response = request.responseText ? JSON.parse(request.responseText) : {};
      } catch {
        response = { raw: request.responseText };
      }
      if (request.status < 200 || request.status >= 300) {
        const detail = response?.detail;
        const message = typeof detail === "string"
          ? detail
          : response?.message || response?.raw || request.statusText || `HTTP ${request.status}`;
        reject(new ApiError(String(message), request.status));
        return;
      }
      resolve(response as T);
    };
    request.send(JSON.stringify(body));
  });
}

function commonVikingRoot(left: string, right: string): string {
  const prefix = "viking://";
  if (!left.startsWith(prefix) || !right.startsWith(prefix)) return "";
  const leftParts = left.slice(prefix.length).split("/").filter(Boolean);
  const rightParts = right.slice(prefix.length).split("/").filter(Boolean);
  const common: string[] = [];
  for (let index = 0; index < Math.min(leftParts.length, rightParts.length); index += 1) {
    if (leftParts[index] !== rightParts[index]) break;
    common.push(leftParts[index]);
  }
  return common.length ? `${prefix}${common.join("/")}` : "";
}

function knowledgeWorkspaceRoot(sourceRoot: string, wikiRoot: string): string {
  const commonRoot = commonVikingRoot(sourceRoot, wikiRoot);
  const legacyLayout = sourceRoot === `${commonRoot}/input` && wikiRoot === `${commonRoot}/output`;
  const knowledgeLayout = sourceRoot === `${commonRoot}/input/raw_knowledge_base`
    && wikiRoot === `${commonRoot}/output/processed_knowledge`;
  return legacyLayout || knowledgeLayout
    ? commonRoot
    : "";
}

export function resolveKnowledgeHref(
  href: string,
  current: KnowledgeContent,
  sourceRoot: string,
  wikiRoot: string,
  sourceTree: TreeResponse | null,
  wikiTree: TreeResponse | null,
): { kind: "source" | "wiki"; entry: TreeEntry; fragment: string } | null {
  const value = href.trim();
  if (!value || value.startsWith("#") || /^(?:https?:|mailto:|tel:|data:|javascript:)/i.test(value)) return null;
  const hashIndex = value.indexOf("#");
  const rawPath = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const rawFragment = hashIndex >= 0 ? value.slice(hashIndex + 1) : "";
  const rawPathWithoutQuery = rawPath.split("?", 1)[0].trim();
  let path = rawPathWithoutQuery;
  let fragment = rawFragment;
  try {
    path = decodeURIComponent(rawPathWithoutQuery);
  } catch {
    // Keep the original path when a document contains a malformed escape.
  }
  try {
    fragment = decodeURIComponent(rawFragment);
  } catch {
    // Keep the original fragment when a document contains a malformed escape.
  }
  if (!path) return null;

  let candidate = path;
  if (!path.startsWith("viking://")) {
    const logicalRoot = current.kind === "wiki" ? wikiRoot : sourceRoot;
    const workspaceRoot = knowledgeWorkspaceRoot(sourceRoot, wikiRoot);
    const root = path.startsWith("/") || !workspaceRoot || !current.uri.startsWith(`${workspaceRoot}/`)
      ? logicalRoot
      : workspaceRoot;
    const currentRelative = current.uri.startsWith(`${root}/`) ? current.uri.slice(root.length + 1) : "";
    const parts = path.startsWith("/") ? [] : currentRelative.split("/").slice(0, -1).filter(Boolean);
    for (const part of path.replace(/^\/+/, "").split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") {
        if (!parts.length) return null;
        parts.pop();
      } else {
        parts.push(part);
      }
    }
    candidate = `${root}/${parts.join("/")}`;
  }
  candidate = candidate.replace(/\/+$/, "");
  const candidates = [candidate];
  if (!/\.[^/]+$/.test(candidate)) candidates.push(`${candidate}.md`, `${candidate}/index.md`);
  const trees = current.kind === "wiki"
    ? ([wikiTree, sourceTree] as const)
    : ([sourceTree, wikiTree] as const);
  for (const tree of trees) {
    const entry = tree?.entries.find((item) => !item.is_dir && candidates.includes(item.uri));
    if (entry) return { kind: tree?.kind || current.kind, entry, fragment };
  }
  return null;
}

function formatBytes(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return "";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function taskTone(status = ""): "green" | "amber" | "red" | "blue" | "gray" {
  if (status === "completed") return "green";
  if (status === "failed" || status === "cancelled") return "red";
  if (ACTIVE_TASK_STATES.has(status)) return "blue";
  return "gray";
}

function taskLabel(status = "") {
  return ({
    accepted: "已受理",
    pending: "等待中",
    queued: "排队中",
    running: "挖掘中",
    committing: "写入知识库",
    cancelling: "停止中",
    cancelled: "已停止",
    completed: "已完成",
    failed: "失败",
  } as Record<string, string>)[status] || status || "未知";
}

function formatCompileTime(value?: string | null) {
  if (!value) return "首次运行 · 全量编译";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function buildTree(entries: TreeEntry[], rootUri: string): TreeNode[] {
  const byUri = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const entry of entries) byUri.set(entry.uri, { ...entry, children: [] });
  for (const entry of entries) {
    const node = byUri.get(entry.uri)!;
    const parentUri = entry.uri.slice(0, entry.uri.lastIndexOf("/"));
    const parent = byUri.get(parentUri);
    if (parent && parent.uri !== rootUri) parent.children.push(node);
    else roots.push(node);
  }
  const sortNodes = (nodes: TreeNode[]) => {
    nodes.sort((left, right) => Number(right.is_dir) - Number(left.is_dir) || left.name.localeCompare(right.name));
    nodes.forEach((node) => sortNodes(node.children));
  };
  sortNodes(roots);
  return roots;
}

function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  if (!query) return nodes;
  const lowered = query.toLowerCase();
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.name.toLowerCase().includes(lowered) || children.length
      ? [{ ...node, children }]
      : [];
  });
}

function TreeList({
  tree,
  emptyText,
  selectedUri,
  selectedDirectoryUri,
  query,
  onSelect,
}: {
  tree: TreeResponse | null;
  emptyText: string;
  selectedUri: string;
  selectedDirectoryUri: string;
  query: string;
  onSelect: (entry: TreeEntry) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const selectedRowRef = useRef<HTMLButtonElement | null>(null);
  const lastScrolledUriRef = useRef("");
  const nodes = useMemo(
    () => filterTree(buildTree(tree?.entries || [], tree?.root_uri || ""), query.trim()),
    [query, tree],
  );
  const ancestorDirs = useMemo(() => {
    const paths = new Map<string, string[]>();
    const walk = (items: TreeNode[], ancestors: string[]) => {
      for (const item of items) {
        paths.set(item.uri, ancestors);
        if (item.children.length) walk(item.children, [...ancestors, item.uri]);
      }
    };
    walk(buildTree(tree?.entries || [], tree?.root_uri || ""), []);
    return paths;
  }, [tree]);

  // File jumps (wiki links, history back/forward) only change selectedUri, so
  // expand the selected file's ancestor directories here to keep it visible.
  useEffect(() => {
    if (!selectedUri) return;
    const ancestors = ancestorDirs.get(selectedUri);
    if (!ancestors?.length) return;
    setExpanded((previous) => {
      if (ancestors.every((uri) => previous.has(uri))) return previous;
      const next = new Set(previous);
      for (const uri of ancestors) next.add(uri);
      return next;
    });
  }, [selectedUri, ancestorDirs]);

  // Scroll to the selected row once it is rendered (the expansion above may
  // need a re-render first). Only once per selection change, so manual tree
  // browsing and background tree refreshes do not yank the scroll position.
  useEffect(() => {
    if (!selectedUri || lastScrolledUriRef.current === selectedUri) return;
    const row = selectedRowRef.current;
    if (!row) return;
    lastScrolledUriRef.current = selectedUri;
    // Center the row within the tree's own scroll container so the user can
    // see where the jump landed. Scroll only that container (not every
    // scrollable ancestor) so the rest of the page does not move.
    let container = row.parentElement;
    while (container) {
      const style = window.getComputedStyle(container);
      if (
        (style.overflowY === "auto" || style.overflowY === "scroll") &&
        container.scrollHeight > container.clientHeight
      ) {
        break;
      }
      container = container.parentElement;
    }
    if (container) {
      const rowRect = row.getBoundingClientRect();
      const boxRect = container.getBoundingClientRect();
      container.scrollTop += rowRect.top - boxRect.top - (boxRect.height - rowRect.height) / 2;
    } else {
      row.scrollIntoView?.({ block: "center" });
    }
  }, [selectedUri, expanded]);

  if (!tree) {
    return <div className="grid h-full min-h-40 place-items-center text-xs text-muted-foreground">正在读取目录…</div>;
  }
  if (!tree.exists || !tree.entries.length) {
    if (!emptyText) return <div className="h-full" aria-hidden="true" />;
    return (
      <div className="grid h-full min-h-40 place-items-center px-6 text-center text-xs leading-5 text-muted-foreground">
        {emptyText}
      </div>
    );
  }

  function toggle(uri: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  }

  function renderNode(node: TreeNode, depth: number): React.ReactNode {
    const open = expanded.has(node.uri) || Boolean(query.trim());
    const Icon = node.is_dir ? Folder : File;
    return (
      <div key={node.uri}>
        <button
          ref={selectedUri === node.uri ? selectedRowRef : undefined}
          type="button"
          role="treeitem"
          aria-expanded={node.is_dir ? open : undefined}
          title={node.uri}
          className={cn(
            "group flex min-h-8 w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-accent-soft/60",
            selectedUri === node.uri && "bg-accent-soft text-accent",
            node.is_dir && selectedDirectoryUri === node.uri && "ring-1 ring-inset ring-accent/45",
          )}
          style={{ paddingLeft: `${8 + depth * 16}px` }}
          onClick={() => {
            if (node.is_dir) toggle(node.uri);
            onSelect(node);
          }}
        >
          {node.is_dir
            ? open ? <ChevronDown className="size-3 text-muted-soft" /> : <ChevronRight className="size-3 text-muted-soft" />
            : <span className="w-3" />}
          <Icon className={cn("size-3.5 shrink-0", node.is_dir ? "text-accent" : "text-slate-500")} />
          <span className="min-w-0 flex-1 truncate font-medium">{node.name}</span>
          {!node.is_dir && node.size != null && (
            <span className="shrink-0 text-[10px] text-muted-soft">{formatBytes(node.size)}</span>
          )}
        </button>
        {node.is_dir && open && node.children.map((child) => renderNode(child, depth + 1))}
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto p-2" role="tree">
      {nodes.length ? nodes.map((node) => renderNode(node, 0)) : (
        <div className="grid min-h-40 place-items-center text-xs text-muted-foreground">没有匹配的文件</div>
      )}
    </div>
  );
}

function taskLogLines(task: MiningTask | null): string[] {
  if (!task) return ["等待启动知识库挖掘任务…"];
  const lines: string[] = [];
  lines.push(`[task] ${task.task_id}`);
  lines.push(`[status] ${task.status || "unknown"}${task.stage ? ` · ${task.stage}` : ""}`);
  for (const event of task.events || []) {
    const time = String(event.created_at || event.timestamp || event.ts || "");
    const name = String(event.event || event.type || event.status || "event");
    const message = String(event.message || event.detail || event.stage || "");
    lines.push(`${time ? `[${time}] ` : ""}${name}${message ? ` · ${message}` : ""}`);
  }
  if (task.error) {
    lines.push(`[error] ${typeof task.error === "string" ? task.error : JSON.stringify(task.error, null, 2)}`);
  }
  if (task.result && TERMINAL_TASK_STATES.has(String(task.status || ""))) {
    lines.push("[result]");
    lines.push(JSON.stringify(task.result, null, 2));
  }
  return lines;
}

export default function KnowledgeBaseMiningView({ active }: { active: boolean }) {
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

  if (!active) return null;

  const activeKind: "source" | "wiki" = workspaceTab === "wiki" ? "wiki" : "source";
  const activeTree = activeKind === "source" ? sourceTree : wikiTree;
  const activeRoot = activeKind === "source" ? config?.source_root : wikiRoot;
  const activeUploadParent = selectedDirectoryUri || activeRoot || "";

  return (
    <div className="px-[22px] pb-8 pt-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-[14px] border border-border bg-white px-4 py-3 shadow-[var(--shadow-soft)]">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-accent-soft text-accent">
            <Server className="size-4.5" />
          </div>
          <div id="knowledge-mining-account-content" className="contents">
            {accountPanelOpen ? (
              <>
              <div>
                <Label htmlFor="knowledge-mining-account" className="text-[11px] font-bold text-muted-foreground">
                  OpenViking 账号
                </Label>
                <select
                  id="knowledge-mining-account"
                  value={account}
                  onChange={(event) => setAccount(event.target.value)}
                  className="mt-1 h-9 min-w-[240px] rounded-md border border-input bg-background px-3 text-sm font-semibold outline-none focus:ring-2 focus:ring-ring"
                >
                  {(config?.accounts || []).map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
              </div>
              <div className="min-w-0 text-[11px] leading-5 text-muted-foreground">
                <div className="truncate">{config?.endpoint || "尚未配置 OpenViking Endpoint"}</div>
                <div className="flex items-center gap-2">
                  <Pill tone={config?.account_source === "openviking" ? "green" : "amber"}>
                    {config?.account_source === "openviking" ? "账号已同步" : "本地配置"}
                  </Pill>
                  <span>先选账号，再管理来源并启动 Compile</span>
                </div>
              </div>
              </>
            ) : (
              <div className="min-w-0">
                <div className="text-[11px] font-bold text-muted-foreground">OpenViking 账号</div>
                <div className="truncate text-sm font-semibold text-foreground">{account || "尚未选择"}</div>
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={loading || loadingWikiGraph || !account}
            onClick={() => {
              void loadWorkspace(true);
              if (workspaceTab === "wiki" && wikiViewMode === "graph") void loadWikiGraph();
            }}
          >
            <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 刷新
          </Button>
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={accountPanelOpen}
            aria-controls="knowledge-mining-account-content"
            aria-label={accountPanelOpen ? "收起 OpenViking 账号区域" : "展开 OpenViking 账号区域"}
            title={accountPanelOpen ? "收起账号区域" : "展开账号区域"}
            onClick={() => setAccountPanelOpen((open) => !open)}
          >
            <ChevronDown className={cn("size-4 transition-transform", accountPanelOpen && "rotate-180")} />
          </Button>
        </div>
      </div>

      {!account ? (
        <div className="grid min-h-[420px] place-items-center rounded-[14px] border border-dashed border-border bg-white text-sm text-muted-foreground">
          没有可用的 OpenViking 账号，请先在“运行状态”中完成连接配置。
        </div>
      ) : (
        <div>
          <div className="mb-3 flex items-center gap-1 rounded-[14px] border border-border bg-white p-1 shadow-[var(--shadow-soft)]" role="tablist" aria-label="知识库挖掘工作区">
            {([
              ["source", "知识源", FolderInput],
              ["wiki", "知识库", BookOpenText],
              ["mining", "挖掘", Play],
            ] as const).map(([key, label, Icon]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={workspaceTab === key}
                className={cn(
                  "flex h-9 min-w-[112px] items-center justify-center gap-2 rounded-[10px] px-4 text-xs font-bold transition-colors",
                  workspaceTab === key ? "bg-foreground text-white" : "text-muted-foreground hover:bg-surface-subtle hover:text-foreground",
                )}
                onClick={() => switchWorkspaceTab(key)}
              >
                <Icon className="size-3.5" /> {label}
              </button>
            ))}
            {workspaceTab === "wiki" && (
              <div className="ml-auto flex items-center gap-2">
                <select
                  aria-label="知识库版本"
                  value={wikiRoot}
                  onChange={(event) => setWikiRoot(event.target.value)}
                  className="h-8 max-w-[360px] rounded-md border border-input bg-background px-2 text-[11px] font-semibold"
                  title={wikiRoot}
                >
                  <option value={config?.wiki_root || ""}>当前工作区 · {config?.wiki_root}</option>
                  {wikiRoots.map((item) => (
                    <option key={item.uri} value={item.uri}>
                      历史产物 · {item.name} · {item.page_count} 页
                    </option>
                  ))}
                </select>
                <div className="flex rounded-[9px] border border-border bg-surface-subtle p-0.5" role="group" aria-label="知识库查看方式">
                <button
                  type="button"
                  className={cn(
                    "flex h-7 items-center gap-1.5 rounded-[7px] px-2.5 text-[11px] font-semibold",
                    wikiViewMode === "files" ? "bg-white text-foreground shadow-sm" : "text-muted-foreground",
                  )}
                  onClick={() => setWikiViewMode("files")}
                >
                  <File className="size-3" /> 文件
                </button>
                <button
                  type="button"
                  className={cn(
                    "flex h-7 items-center gap-1.5 rounded-[7px] px-2.5 text-[11px] font-semibold",
                    wikiViewMode === "graph" ? "bg-white text-foreground shadow-sm" : "text-muted-foreground",
                  )}
                  onClick={() => setWikiViewMode("graph")}
                >
                  <Network className="size-3" /> HTML 图谱
                </button>
                </div>
              </div>
            )}
          </div>

          {workspaceTab !== "mining" ? (
            workspaceTab === "wiki" && wikiViewMode === "graph" ? (
              <Panel
                className="flex h-[calc(100vh-245px)] min-h-[560px] flex-col"
                title={<span className="flex items-center gap-2"><Network className="size-4 text-accent" />知识库 HTML 图谱</span>}
                count={wikiGraph ? `${wikiGraph.page_count} 节点 · ${wikiGraph.edge_count} 关系` : undefined}
                extra={wikiGraph && (
                  <span className="text-[10px] text-muted-foreground">
                    数据来自 OpenViking API · 本地 HTML 渲染
                  </span>
                )}
              >
                <div className="min-h-0 flex-1 bg-[#f4f6f8]">
                  {loadingWikiGraph ? (
                    <div className="grid h-full place-items-center text-xs text-muted-foreground">
                      <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" />正在通过 OpenViking 读取完整知识库并生成图谱…</span>
                    </div>
                  ) : wikiGraphError ? (
                    <div className="grid h-full place-items-center px-8 text-center text-xs leading-6 text-muted-foreground">
                      <div>
                        <Network className="mx-auto mb-3 size-8 text-muted-soft" />
                        <div className="font-semibold text-foreground">图谱暂不可用</div>
                        <div className="mt-1">{wikiGraphError}</div>
                        <Button className="mt-4" variant="outline" size="sm" onClick={() => void loadWikiGraph()}>
                          <RefreshCw className="size-3.5" /> 重新生成
                        </Button>
                      </div>
                    </div>
                  ) : wikiGraph ? (
                    <iframe
                      title="知识库 HTML 图谱"
                      srcDoc={wikiGraph.html}
                      sandbox="allow-scripts"
                      className="h-full w-full border-0 bg-white"
                    />
                  ) : (
                    <div className="grid h-full place-items-center text-xs text-muted-foreground">尚未生成图谱</div>
                  )}
                </div>
              </Panel>
            ) : (
            <div className="grid h-[calc(100vh-245px)] min-h-[560px] grid-cols-[minmax(270px,0.38fr)_minmax(0,1fr)] gap-3">
              <Panel
                className="flex min-h-0 flex-col"
                title={(
                  <span className="flex items-center gap-2">
                    {activeKind === "source" ? <FolderInput className="size-4 text-accent" /> : <BookOpenText className="size-4 text-accent" />}
                    {activeKind === "source" ? "知识源" : "知识库"}
                  </span>
                )}
                count={activeTree?.exists ? `${activeTree.entries.length} 项` : activeKind === "wiki" ? "尚未生成" : undefined}
                extra={(
                  <div className="flex items-center gap-1.5">
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      accept={KNOWLEDGE_UPLOAD_ACCEPT}
                      className="hidden"
                      onChange={(event) => void uploadFiles(Array.from(event.target.files || []))}
                    />
                    <input
                      ref={folderInputRef}
                      type="file"
                      multiple
                      accept={KNOWLEDGE_UPLOAD_ACCEPT}
                      className="hidden"
                      {...({ webkitdirectory: "", directory: "" } as any)}
                      onChange={(event) => void uploadFiles(Array.from(event.target.files || []))}
                    />
                    <Button variant="outline" size="sm" disabled={uploading} title={`上传文件到${activeKind === "source" ? "知识源" : "知识库"}`} onClick={() => fileInputRef.current?.click()}>
                      <Upload className="size-3.5" /> 文件
                    </Button>
                    <Button variant="outline" size="sm" disabled={uploading} title={`上传文件夹到${activeKind === "source" ? "知识源" : "知识库"}`} onClick={() => folderInputRef.current?.click()}>
                      <FolderInput className="size-3.5" /> 文件夹
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!selectedTreeEntry || deleting || uploading}
                      title={selectedTreeEntry ? `删除 ${selectedTreeEntry.name}` : "请先选择要删除的文件或文件夹"}
                      onClick={() => void deleteSelectedEntry()}
                    >
                      {deleting ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                      删除
                    </Button>
                  </div>
                )}
              >
                <div className="flex min-h-0 flex-1 flex-col">
                  <div className="border-b border-line bg-background/60 px-3 py-2">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        className={cn(
                          "shrink-0 rounded-md border px-2 py-1 text-[10px] font-bold",
                          activeUploadParent === activeRoot
                            ? "border-accent/50 bg-accent-soft text-accent"
                            : "border-border bg-white text-muted-foreground hover:text-foreground",
                        )}
                        onClick={() => {
                          setSelectedDirectoryUri("");
                          if (selectedTreeEntry?.is_dir) setSelectedTreeEntry(null);
                        }}
                      >
                        根目录
                      </button>
                      <span className="shrink-0 text-[10px] text-muted-foreground">上传到</span>
                      <code className="block min-w-0 flex-1 truncate text-[11px] font-semibold text-foreground">
                        {activeUploadParent}
                      </code>
                    </div>
                    {uploadProgress && (
                      <div className="mt-2" aria-live="polite">
                        <div
                          className="h-1.5 overflow-hidden rounded-full bg-muted"
                          role="progressbar"
                          aria-label="知识文件上传进度"
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={uploadProgress.percent}
                        >
                          <div
                            className="h-full rounded-full bg-accent transition-[width] duration-200"
                            style={{ width: `${uploadProgress.percent}%` }}
                          />
                        </div>
                        <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
                          <span className="font-semibold text-foreground">{uploadProgress.label}</span>
                          {uploadProgress.currentFile && <span className="min-w-0 flex-1 truncate">{uploadProgress.currentFile}</span>}
                          <span className="ml-auto shrink-0">
                            {uploadProgress.completedFiles}/{uploadProgress.totalFiles} · {uploadProgress.percent}%
                          </span>
                        </div>
                      </div>
                    )}
                  </div>
                  <div className="border-b border-line p-2">
                    <div className="flex h-8 items-center gap-2 rounded-lg border border-input bg-white px-2.5">
                      <Search className="size-3.5 text-muted-soft" />
                      <input
                        value={treeQuery}
                        onChange={(event) => setTreeQuery(event.target.value)}
                        className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-soft"
                        placeholder="搜索文件…"
                      />
                    </div>
                  </div>
                  <div className="min-h-0 flex-1">
                    <TreeList
                      tree={activeTree}
                      emptyText={activeKind === "source" ? "暂无知识源，可上传文件或文件夹。" : "暂无知识库文件，可上传文件或执行知识库挖掘。"}
                      selectedUri={selectedTreeEntry?.uri || (selectedContent?.kind === activeKind ? selectedContent.uri : "")}
                      selectedDirectoryUri={activeUploadParent}
                      query={treeQuery}
                      onSelect={(entry) => {
                        setSelectedTreeEntry(entry);
                        if (entry.is_dir) {
                          setSelectedDirectoryUri(entry.uri);
                        } else {
                          void openFile(activeKind, entry);
                        }
                      }}
                    />
                  </div>
                </div>
              </Panel>

              <Panel
                className="flex min-h-0 flex-col"
                title={(
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="flex shrink-0 items-center rounded-md border border-border bg-surface-subtle p-0.5" role="group" aria-label="文件浏览历史">
                      <button
                        type="button"
                        aria-label="后退到上一个文件"
                        title="后退到上一个文件"
                        disabled={!canGoBack || loadingContent}
                        className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-white hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
                        onClick={() => void navigateFileHistory(-1)}
                      >
                        <ArrowLeft className="size-3.5" />
                      </button>
                      <button
                        type="button"
                        aria-label="前进到下一个文件"
                        title="前进到下一个文件"
                        disabled={!canGoForward || loadingContent}
                        className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-white hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
                        onClick={() => void navigateFileHistory(1)}
                      >
                        <ArrowRight className="size-3.5" />
                      </button>
                    </span>
                    <File className="size-4 shrink-0 text-accent" />
                    <span className="truncate">{selectedContent?.name || "文件内容"}</span>
                  </span>
                )}
                extra={selectedContent ? (
                  <div className="flex items-center gap-1.5">
                    {selectedIsMarkdown && (
                      <div className="flex rounded-lg border border-border bg-surface-subtle p-0.5">
                        <button
                          type="button"
                          className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", editorMode === "edit" && "bg-white shadow-sm")}
                          onClick={() => setEditorMode("edit")}
                        >
                          <PencilLine className="size-3" /> 编辑
                        </button>
                        <button
                          type="button"
                          className={cn("flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-semibold", editorMode === "preview" && "bg-white shadow-sm")}
                          onClick={() => setEditorMode("preview")}
                        >
                          <Eye className="size-3" /> 预览
                        </button>
                      </div>
                    )}
                    <Button variant="outline" size="sm" onClick={() => setVersionPanelOpen(true)}>
                      <History className="size-3.5" />
                      版本
                    </Button>
                    {selectedContent.editable ? (
                      <Button size="sm" disabled={!contentDirty || savingContent} onClick={() => void saveFile()}>
                        {savingContent ? <LoaderCircle className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
                        {savingContent ? "保存中" : "保存"}
                      </Button>
                    ) : <Pill tone="gray">只读</Pill>}
                  </div>
                ) : undefined}
              >
                <div className="flex min-h-0 flex-1 flex-col">
                  {selectedContent && (
                    <div className="flex items-center gap-2 border-b border-line bg-background/60 px-3 py-2 text-[11px] text-muted-foreground">
                      <code className="min-w-0 flex-1 truncate">{selectedContent.uri}</code>
                      {contentDirty && <span className="shrink-0 font-semibold text-accent">未保存</span>}
                    </div>
                  )}
                  <div className="min-h-0 flex-1">
                    {loadingContent ? (
                      <div className="grid h-full place-items-center text-xs text-muted-foreground">
                        <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" /> 正在打开文件…</span>
                      </div>
                    ) : !selectedContent ? (
                      <div className="grid h-full place-items-center text-center text-xs leading-6 text-muted-foreground">
                        <div><File className="mx-auto mb-2 size-8 text-muted-soft" />从左侧目录选择文件</div>
                      </div>
                    ) : selectedIsMarkdown && editorMode === "preview" ? (
                      <div className="h-full overflow-auto bg-white px-7 py-6">
                        <MarkdownDocument
                          content={editorValue}
                          onLinkClick={handleMarkdownLink}
                          anchor={previewAnchor}
                          onAnchorHandled={() => setPreviewAnchor("")}
                        />
                      </div>
                    ) : selectedContent.editable ? (
                      <textarea
                        value={editorValue}
                        onChange={(event) => setEditorValue(event.target.value)}
                        spellCheck={false}
                        className="h-full w-full resize-none border-0 bg-[#fbfcfe] p-5 font-mono text-xs leading-6 outline-none"
                        aria-label={`${selectedContent.name} 文件编辑器`}
                      />
                    ) : (
                      <pre className="h-full overflow-auto whitespace-pre-wrap break-words bg-[#fbfcfe] p-5 font-mono text-xs leading-6">{editorValue}</pre>
                    )}
                  </div>
                  {selectedContent?.kind === "wiki" && selectedIsMarkdown && (
                    <section
                      className="shrink-0 border-t border-line bg-surface-subtle/70"
                      aria-label="反向引用"
                    >
                      <button
                        type="button"
                        className={cn(
                          "flex h-9 w-full items-center gap-2 px-4 text-left transition-colors hover:bg-white/70",
                          backlinksOpen && "border-b border-line/70",
                        )}
                        aria-expanded={backlinksOpen}
                        aria-controls="knowledge-backlinks-content"
                        onClick={() => setBacklinksOpen((open) => !open)}
                      >
                        <Link2 className="size-3.5 text-accent" />
                        <span className="text-[11px] font-bold text-foreground">被引用</span>
                        <span className="rounded-full bg-accent-soft px-1.5 py-0.5 text-[10px] font-bold text-accent">
                          {selectedContent.backlinks?.length || 0}
                        </span>
                        <span className="ml-auto text-[10px] text-muted-foreground">独立于正文，由 Wiki 链接索引生成</span>
                        <ChevronDown className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", backlinksOpen && "rotate-180")} />
                      </button>
                      {backlinksOpen && (
                        <div id="knowledge-backlinks-content">
                          {selectedContent.link_index?.status === "error" ? (
                            <div className="px-4 py-3 text-[11px] text-red-600">
                              反向引用索引暂不可用：{selectedContent.link_index.message || "重算失败"}
                            </div>
                          ) : selectedContent.link_index?.status === "pending" ? (
                            <div className="px-4 py-3 text-[11px] text-muted-foreground">
                              反向引用索引正在后台更新，不影响文件阅读和保存。
                            </div>
                          ) : selectedContent.backlinks?.length ? (
                            <div className="max-h-40 overflow-auto p-2">
                              {selectedContent.backlinks.map((backlink) => (
                                <button
                                  key={backlink.source_uri}
                                  type="button"
                                  title={`打开 ${backlink.source_uri}`}
                                  className="group flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left hover:bg-white"
                                  onClick={() => void openFile("wiki", {
                                    uri: backlink.source_uri,
                                    name: backlink.source_name,
                                    is_dir: false,
                                  })}
                                >
                                  <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-white text-accent shadow-sm">
                                    <File className="size-3.5" />
                                  </span>
                                  <span className="min-w-0 flex-1">
                                    <span className="block truncate text-[11px] font-bold text-foreground group-hover:text-accent">
                                      {backlink.source_name}
                                    </span>
                                    <span className="block truncate text-[10px] text-muted-foreground">
                                      {backlink.source_path}
                                      {backlink.labels?.length ? ` · ${backlink.labels.join("、")}` : ""}
                                    </span>
                                  </span>
                                  <span className="shrink-0 text-[10px] font-semibold text-muted-foreground">
                                    {backlink.count > 1 ? `${backlink.count} 处引用` : "打开"}
                                  </span>
                                  <ChevronRight className="size-3.5 shrink-0 text-muted-soft group-hover:text-accent" />
                                </button>
                              ))}
                            </div>
                          ) : (
                            <div className="px-4 py-3 text-[11px] text-muted-foreground">当前没有其他 Wiki 文件引用此页。</div>
                          )}
                        </div>
                      )}
                    </section>
                  )}
                  {selectedContent && (
                    <div className="flex h-8 items-center border-t border-line bg-surface-subtle px-3 text-[10px] text-muted-foreground">
                      <span>{selectedContent.editable ? "可编辑" : "当前类型只读"}</span>
                      <span className="ml-auto">{editorValue.split("\n").length} 行 · {editorValue.length.toLocaleString()} 字符</span>
                    </div>
                  )}
                </div>
              </Panel>
            </div>
            )
          ) : (
            <Panel
              className="flex h-[calc(100vh-245px)] min-h-[560px] flex-col"
              title={<span className="flex items-center gap-2"><Play className="size-4 text-accent" />挖掘控制台</span>}
              extra={selectedTask && <Pill tone={taskTone(currentStatus)}>{taskLabel(currentStatus)}</Pill>}
            >
              <div className="grid min-h-0 flex-1 grid-cols-1 xl:grid-cols-[minmax(360px,0.82fr)_minmax(0,1.18fr)]">
                <div className="space-y-3 overflow-auto border-b border-line p-4 xl:border-b-0 xl:border-r">
                  <div className="rounded-xl border border-border bg-surface-subtle px-3 py-2 text-[11px] text-foreground">
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-bold">OpenViking Compile</span>
                      <span>
                        {capabilities?.can_create
                          ? capabilities.probe_supported === false
                            ? "接口已接入 · 当前 OV 不支持预检"
                            : "接口可用"
                          : capabilities
                            ? `不可用 · ${capabilities.reason_code || "未配置"}`
                            : "正在检测…"}
                      </span>
                    </div>
                    <div className="mt-1.5 flex items-center justify-between gap-3 border-t border-line pt-1.5 text-[10px] text-muted-foreground">
                      <span>增量编译起点</span>
                      <span className="truncate" title={capabilities?.last_compile_time || ""}>
                        {formatCompileTime(capabilities?.last_compile_time)}
                      </span>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div className="rounded-xl border border-border bg-surface-subtle p-3">
                      <div className="text-[10px] font-bold uppercase tracking-wider text-accent">FROM</div>
                      <code className="mt-1 block truncate text-[11px] font-semibold">{config?.source_root}</code>
                    </div>
                    <div className="rounded-xl border border-border bg-surface-subtle p-3">
                      <div className="text-[10px] font-bold uppercase tracking-wider text-accent">TO</div>
                      <code className="mt-1 block truncate text-[11px] font-semibold">{config?.wiki_root}</code>
                    </div>
                  </div>
                  <div>
                    <Label htmlFor="knowledge-instruction" className="text-[11px] font-bold">本次挖掘要求</Label>
                    <Textarea
                      id="knowledge-instruction"
                      value={instruction}
                      onChange={(event) => setInstruction(event.target.value)}
                      className="mt-1 min-h-[140px] resize-none text-xs leading-5"
                      placeholder="例如：按产品模块整理，保留来源引用，并突出常见问题。"
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Button
                        className="flex-1"
                        disabled={starting || running || !sourceTree?.entries.length || !capabilities?.can_create}
                        onClick={() => void startMining()}
                      >
                        {starting ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}
                        {starting ? "正在启动" : running ? "任务运行中" : "知识库挖掘"}
                      </Button>
                      <Button variant="outline" disabled={!running || stopping} onClick={() => void stopMining()}>
                        {stopping ? <LoaderCircle className="size-4 animate-spin" /> : <CircleStop className="size-4" />}
                        停止
                      </Button>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <Button type="button" variant="outline">成功经验挖掘</Button>
                      <Button type="button" variant="outline">团队记忆挖掘</Button>
                    </div>
                  </div>
                </div>

                <div className="flex min-h-0 flex-col">
                  <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
                    <div className="text-[12px] font-bold">任务与日志</div>
                    <select
                      value={selectedTaskId}
                      onChange={(event) => setSelectedTaskId(event.target.value)}
                      className="h-8 max-w-[260px] rounded-md border border-input bg-background px-2 text-[11px]"
                    >
                      {!tasks.length && <option value="">暂无任务</option>}
                      {tasks.map((task) => (
                        <option key={task.task_id} value={task.task_id}>
                          {taskLabel(String(task.status || ""))} · {task.task_id}
                        </option>
                      ))}
                    </select>
                  </div>
                  {selectedTask?.progress && (
                    <div className="border-b border-line bg-surface-subtle px-4 py-3">
                      <div className="mb-1.5 flex items-center justify-between gap-3 text-[11px]">
                        <span className="font-semibold text-foreground">{selectedTask.progress.label}</span>
                        <span className="shrink-0 text-muted-foreground">
                          {selectedTask.progress.percent.toFixed(0)}%
                          {selectedTask.progress.mode === "stage" ? " · 阶段进度" : ""}
                        </span>
                      </div>
                      <div
                        className="h-2 overflow-hidden rounded-full bg-border"
                        role="progressbar"
                        aria-label="OpenViking Compile 进度"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={selectedTask.progress.percent}
                      >
                        <div
                          className={cn(
                            "h-full rounded-full transition-[width] duration-500",
                            currentStatus === "failed" || currentStatus === "cancelled" ? "bg-destructive" : "bg-accent",
                          )}
                          style={{ width: `${selectedTask.progress.percent}%` }}
                        />
                      </div>
                      {selectedTask.progress.mode === "stage" && running && (
                        <div className="mt-1.5 text-[10px] text-muted-foreground">
                          OpenViking 暂未返回文件级百分比，当前按真实 Compile 阶段展示。
                        </div>
                      )}
                    </div>
                  )}
                  <pre
                    ref={logRef}
                    className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words bg-[#111318] p-4 font-mono text-[11px] leading-5 text-[#d7dce5]"
                  >
                    {logs.join("\n")}
                  </pre>
                </div>
              </div>
            </Panel>
          )}
        </div>
      )}

      {versionPanelOpen && selectedContent && (
        <div
          className="fixed inset-0 z-[80] flex justify-end bg-black/45"
          role="dialog"
          aria-modal="true"
          aria-label={`${selectedContent.name} 版本历史`}
          onMouseDown={() => setVersionPanelOpen(false)}
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
                <div className="truncate text-base font-bold">版本历史 · {selectedContent.name}</div>
                <code className="mt-0.5 block truncate text-[10px] text-muted-foreground">{selectedContent.uri}</code>
              </div>
              <button
                type="button"
                className="grid size-9 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-surface-subtle hover:text-foreground"
                aria-label="关闭版本历史"
                onClick={() => setVersionPanelOpen(false)}
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
      )}
    </div>
  );
}
