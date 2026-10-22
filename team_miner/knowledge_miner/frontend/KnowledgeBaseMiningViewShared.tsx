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

export interface MiningConfig {
  accounts: string[];
  current: string;
  source_root: string;
  wiki_root: string;
  skill_uri: string;
  endpoint: string;
  account_source: string;
  error?: string;
}

export interface TreeEntry {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | null;
  modified_at?: string;
  abstract?: string;
}

export interface TreeResponse {
  account: string;
  kind: "source" | "wiki";
  root_uri: string;
  exists: boolean;
  entries: TreeEntry[];
}

export interface CompileCapabilities {
  account: string;
  configured: boolean | null;
  can_create: boolean;
  probe_supported?: boolean;
  reason_code?: string | null;
  last_compile_time?: string | null;
  incremental?: boolean;
}

export interface CompileProgress {
  percent: number;
  mode: "reported" | "stage";
  stage: string;
  label: string;
}

export interface MiningTask {
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

export interface KnowledgeContent {
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

export interface KnowledgeLink {
  target_uri: string;
  target_name: string;
  target_path: string;
  labels: string[];
  lines: number[];
  count: number;
}

export interface KnowledgeBacklink {
  source_uri: string;
  source_name: string;
  source_path: string;
  labels: string[];
  lines: number[];
  count: number;
}

export interface WikiGraphResponse {
  account: string;
  wiki_root: string;
  source: "openviking-api";
  renderer: "local-html-fallback";
  page_count: number;
  edge_count: number;
  generated_at: string;
  html: string;
}

export interface WikiRoot {
  uri: string;
  name: string;
  page_count: number;
  modified_at?: string;
  abstract?: string;
}

export interface FileVisit {
  kind: "source" | "wiki";
  uri: string;
  name: string;
  wiki_root: string;
}

export interface FileNavigation {
  entries: FileVisit[];
  index: number;
}

export interface UploadProgressState {
  percent: number;
  label: string;
  currentFile: string;
  completedFiles: number;
  totalFiles: number;
}

export interface TreeNode extends TreeEntry {
  children: TreeNode[];
}

export type WorkspaceTab = "source" | "wiki" | "mining";

export type EditorMode = "edit" | "preview";

export type WikiViewMode = "files" | "graph";

export const ACTIVE_TASK_STATES = new Set(["accepted", "pending", "queued", "running", "committing", "cancelling"]);

export const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);

export const KNOWLEDGE_UPLOAD_ACCEPT = ".md,.markdown,.txt,.docx,.xlsx,.pptx,.pdf,.csv";

export function uploadJson<T>(
  path: string,
  body: Record<string, unknown>,
  onProgress: (ratio: number) => void,
  headers?: HeadersInit,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    request.timeout = 15 * 60 * 1000;
    request.withCredentials = true;
    tenantHeaders(path, headers || { "Content-Type": "application/json" }).forEach((value, key) => {
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

export function commonVikingRoot(left: string, right: string): string {
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

export function knowledgeWorkspaceRoot(sourceRoot: string, wikiRoot: string): string {
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

export function formatBytes(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return "";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function taskTone(status = ""): "green" | "amber" | "red" | "blue" | "gray" {
  if (status === "completed") return "green";
  if (status === "failed" || status === "cancelled") return "red";
  if (ACTIVE_TASK_STATES.has(status)) return "blue";
  return "gray";
}

export function taskLabel(status = "") {
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

export function formatCompileTime(value?: string | null) {
  if (!value) return "首次运行 · 全量编译";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function buildTree(entries: TreeEntry[], rootUri: string): TreeNode[] {
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

export function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  if (!query) return nodes;
  const lowered = query.toLowerCase();
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.name.toLowerCase().includes(lowered) || children.length
      ? [{ ...node, children }]
      : [];
  });
}

export function TreeList({
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

export function taskLogLines(task: MiningTask | null): string[] {
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
