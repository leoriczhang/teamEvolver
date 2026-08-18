import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Code2,
  ExternalLink,
  Eye,
  FileCode2,
  FileJson,
  FileText,
  Folder,
  FolderOpen,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, type UserProfile, type UsersListResp } from "@/api/client";
import { Empty, Pill } from "@/components/common";
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
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

type WorkspaceMode = "memory" | "skills" | "workspace";
type ScopeName =
  | "personal_memory"
  | "team_memory"
  | "personal_skills"
  | "team_skills"
  | "personal_workspace"
  | "team_workspace";

type ScopeConfig = {
  name: ScopeName;
  root_uri: string;
  space: "personal" | "team";
  kind: "memory" | "skills" | "workspace";
  can_write: boolean;
  openviking_user?: string;
};

type WorkspaceConfig = {
  enabled: boolean;
  deployment: "cloud" | "local" | string;
  endpoint: string;
  studio_url?: string;
  cli_available?: boolean;
  cli_full_access?: boolean;
  personal_access_configured?: boolean;
  user_id: string;
  scopes: Record<ScopeName, ScopeConfig>;
};

type WorkspaceEntry = {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | string | null;
  modified_at?: string;
  abstract?: string;
  relative_path?: string;
};

type TreeResponse = {
  scope: ScopeName;
  root_uri: string;
  uri: string;
  entries: WorkspaceEntry[];
  exists: boolean;
  can_write: boolean;
};

type TreeNode = {
  entry: WorkspaceEntry;
  children: TreeNode[];
};

type CliResult = {
  ok: boolean;
  exit_code: number;
  command: string[];
  stdout: string;
  stderr: string;
  truncated: boolean;
};

type TerminalRecord = {
  id: number;
  command: string;
  result?: CliResult;
  running?: boolean;
};

const MODE_SCOPES: Record<WorkspaceMode, ScopeName[]> = {
  skills: ["personal_skills", "team_skills"],
  memory: ["personal_memory", "team_memory"],
  workspace: [
    "personal_workspace",
    "team_workspace",
    "personal_skills",
    "team_skills",
  ],
};

const SCOPE_LABELS: Record<ScopeName, string> = {
  personal_memory: "个人 Memory",
  team_memory: "团队 Memory",
  personal_skills: "个人 Skill",
  team_skills: "团队 Skill",
  personal_workspace: "个人 Workspace",
  team_workspace: "团队 Workspace",
};

function initialScope(mode: WorkspaceMode, config?: WorkspaceConfig | null): ScopeName {
  const scopes = MODE_SCOPES[mode];
  if (config?.personal_access_configured === false) {
    return scopes.find((name) => name.startsWith("team_")) || scopes[0];
  }
  return scopes[0];
}

function isPersonalScope(scope: ScopeName) {
  return scope.startsWith("personal_");
}

const MARKDOWN_EXTENSIONS = new Set(["md", "markdown", "mdx"]);
const HTML_EXTENSIONS = new Set(["html", "htm"]);
const JSON_EXTENSIONS = new Set(["json", "jsonl"]);
const CODE_EXTENSIONS = new Set([
  "css",
  "go",
  "js",
  "jsx",
  "py",
  "rs",
  "sh",
  "sql",
  "ts",
  "tsx",
  "xml",
  "yaml",
  "yml",
]);

export default function OpenVikingWorkspaceShell({
  active,
  mode,
  user,
}: {
  active: boolean;
  mode: WorkspaceMode;
  user?: UserProfile | null;
}) {
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUserId, setActiveUserId] = useState(
    () => window.localStorage.getItem("teamEvolver.activeUserId") || user?.id || "",
  );
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const [scopeName, setScopeName] = useState<ScopeName>(MODE_SCOPES[mode][0]);
  const [treeResponse, setTreeResponse] = useState<TreeResponse | null>(null);
  const [currentUri, setCurrentUri] = useState("");
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null);
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  const [directoryLevel, setDirectoryLevel] = useState<"l0" | "l1">("l0");
  const [directoryLevels, setDirectoryLevels] = useState({
    l0: "",
    l1: "",
  });
  const [directoryLevelLoading, setDirectoryLevelLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createType, setCreateType] = useState<"file" | "directory">("file");
  const [createName, setCreateName] = useState("");
  const [createContent, setCreateContent] = useState("");

  const scopeNames = MODE_SCOPES[mode];
  const selectedScope = config?.scopes?.[scopeName];
  const isDirty = !!selected && content !== originalContent;
  const tree = useMemo(
    () => buildTree(treeResponse?.entries || [], selectedScope?.root_uri || ""),
    [treeResponse, selectedScope?.root_uri],
  );
  const filteredTree = useMemo(
    () => filterTree(tree, filter.trim().toLowerCase()),
    [filter, tree],
  );

  const loadTree = useCallback(
    async (
      chosenScope: ScopeName,
      chosenUser: string,
      rootUri: string,
      resetSelection = true,
    ) => {
      if (!chosenUser || !rootUri) return;
      setLoading(true);
      try {
        const result = await api<TreeResponse>(
          `/api/openviking/workspace/tree?scope=${encodeURIComponent(chosenScope)}&user_id=${encodeURIComponent(chosenUser)}&uri=${encodeURIComponent(rootUri)}`,
        );
        setTreeResponse(result);
        if (resetSelection) {
          setCurrentUri(result.root_uri);
          setSelected(null);
          setContent("");
          setOriginalContent("");
          setViewMode("preview");
          setDirectoryLevel("l0");
          setDirectoryLevels({ l0: "", l1: "" });
          setFilter("");
        }
        setExpanded(new Set());
      } catch (error: any) {
        toastErr("加载 OpenViking 文件树失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const loadConfig = useCallback(
    async (userId: string) => {
      if (!userId) return;
      setLoading(true);
      try {
        const result = await api<WorkspaceConfig>(
          `/api/openviking/workspace/config?user_id=${encodeURIComponent(userId)}`,
        );
        setConfig(result);
        const nextScope = initialScope(mode, result);
        setScopeName(nextScope);
        const root = result.scopes?.[nextScope]?.root_uri || "";
        setCurrentUri(root);
        if (result.enabled && root) {
          await loadTree(nextScope, userId, root);
        }
      } catch (error: any) {
        toastErr("读取 OpenViking 配置失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [loadTree, mode],
  );

  useEffect(() => {
    if (!active) return;
    api<UsersListResp>("/api/users")
      .then((result) => {
        const list = result.users || [];
        setUsers(list);
        const preferred =
          activeUserId && list.some((item) => item.id === activeUserId)
            ? activeUserId
            : user?.id || list[0]?.id || "";
        setActiveUserId(preferred);
        if (preferred) void loadConfig(preferred);
      })
      .catch((error) => toastErr("加载用户失败", error.message));
  }, [active]);

  useEffect(() => {
    if (!active || !config) return;
    const first = initialScope(mode, config);
    setScopeName(first);
    const root = config.scopes?.[first]?.root_uri;
    if (root) void loadTree(first, activeUserId, root);
  }, [mode]);

  async function chooseScope(next: ScopeName) {
    setScopeName(next);
    const root = config?.scopes?.[next]?.root_uri;
    if (root) await loadTree(next, activeUserId, root);
  }

  async function refreshTree(resetSelection = false) {
    const root = selectedScope?.root_uri;
    if (root) await loadTree(scopeName, activeUserId, root, resetSelection);
  }

  async function openEntry(entry: WorkspaceEntry) {
    if (entry.is_dir) {
      setSelected(entry);
      setCurrentUri(entry.uri);
      setContent("");
      setOriginalContent("");
      setDirectoryLevel("l0");
      setDirectoryLevels({ l0: "", l1: "" });
      setExpanded((previous) => {
        const next = new Set(previous);
        if (next.has(entry.uri)) next.delete(entry.uri);
        else next.add(entry.uri);
        return next;
      });
      setDirectoryLevelLoading(true);
      try {
        const base =
          `/api/openviking/workspace/level?scope=${encodeURIComponent(scopeName)}` +
          `&user_id=${encodeURIComponent(activeUserId)}` +
          `&uri=${encodeURIComponent(entry.uri)}`;
        const [l0, l1] = await Promise.all([
          api<{ content: string }>(`${base}&level=l0`),
          api<{ content: string }>(`${base}&level=l1`),
        ]);
        setDirectoryLevels({
          l0: l0.content || "",
          l1: l1.content || "",
        });
      } catch (error: any) {
        toastErr("读取目录 L0/L1 失败", error.message);
      } finally {
        setDirectoryLevelLoading(false);
      }
      return;
    }
    setSelected(entry);
    setCurrentUri(parentUri(entry.uri));
    setLoading(true);
    try {
      const result = await api<{ content: string }>(
        `/api/openviking/workspace/content?scope=${encodeURIComponent(scopeName)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(result.content || "");
      setOriginalContent(result.content || "");
      setViewMode("preview");
      setDirectoryLevels({ l0: "", l1: "" });
    } catch (error: any) {
      toastErr("读取文件失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  async function saveContent() {
    if (!selected || !selectedScope?.can_write) return;
    setSaving(true);
    try {
      await api("/api/openviking/workspace/content", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: activeUserId,
          scope: scopeName,
          uri: selected.uri,
          content,
          mode: "replace",
        }),
      });
      setOriginalContent(content);
      toastOk("已保存到 OpenViking", selected.name);
      await refreshTree();
    } catch (error: any) {
      toastErr("保存失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  function startCreate(type: "file" | "directory") {
    setCreateType(type);
    setCreateName("");
    setCreateContent(
      type === "file" && mode === "memory" ? "# 新记忆\n\n" : "",
    );
    setCreateOpen(true);
  }

  async function createEntry() {
    const name = createName.trim().replace(/^\/+|\/+$/g, "");
    if (!name) {
      toastErr("请输入名称");
      return;
    }
    const uri = `${currentUri.replace(/\/+$/, "")}/${name}`;
    setSaving(true);
    try {
      if (createType === "directory") {
        await api("/api/openviking/workspace/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: activeUserId,
            scope: scopeName,
            uri,
          }),
        });
      } else {
        await api("/api/openviking/workspace/content", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: activeUserId,
            scope: scopeName,
            uri,
            content: createContent,
            mode: "create",
          }),
        });
      }
      toastOk(createType === "directory" ? "目录已创建" : "文件已创建", name);
      setCreateOpen(false);
      await refreshTree();
    } catch (error: any) {
      toastErr("创建失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteEntry() {
    if (!selected || !selectedScope?.can_write) return;
    if (!window.confirm(`确认删除「${selected.name}」？`)) return;
    try {
      await api(
        `/api/openviking/workspace?scope=${encodeURIComponent(scopeName)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(selected.uri)}`,
        { method: "DELETE" },
      );
      toastOk("已从 OpenViking 删除", selected.name);
      setSelected(null);
      setContent("");
      setOriginalContent("");
      await refreshTree();
    } catch (error: any) {
      toastErr("删除失败", error.message);
    }
  }

  if (!config?.enabled) {
    return (
      <div className="px-5 pb-5">
        <div className="rounded-xl border border-border bg-surface p-8">
          <Empty>
            请先在“运行状态”中启用 OpenViking，并配置本地部署或云端 endpoint。
          </Empty>
        </div>
      </div>
    );
  }

  return (
    <div className="w-full px-4 pb-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border bg-surface px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {scopeNames.map((name) => {
            const unavailable = config.personal_access_configured === false && isPersonalScope(name);
            return (
              <button
                key={name}
                type="button"
                disabled={unavailable}
                title={unavailable ? "请先在用户管理中配置个人 OpenViking 凭证" : undefined}
                onClick={() => void chooseScope(name)}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors",
                  scopeName === name
                    ? "bg-sidebar-primary text-white"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  unavailable && "cursor-not-allowed opacity-45 hover:bg-transparent hover:text-muted-foreground",
                )}
              >
                {SCOPE_LABELS[name]}
                {unavailable ? "（需个人凭证）" : ""}
              </button>
            );
          })}
          <span className="mx-1 h-5 w-px bg-border" />
          <Pill tone={selectedScope?.can_write ? "green" : "gray"}>
            {selectedScope?.can_write ? "可管理" : "只读"}
          </Pill>
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {selectedScope?.root_uri}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {users.length > 1 && (
            <select
              value={activeUserId}
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
          {config.studio_url && (
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
                {treeResponse?.entries.length || 0}
              </span>
            </div>
            {selectedScope?.can_write && (
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  title="新建目录"
                  onClick={() => startCreate("directory")}
                >
                  <Folder className="size-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  title="新建文件"
                  onClick={() => startCreate("file")}
                >
                  <Plus className="size-4" />
                </Button>
              </div>
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
                if (selectedScope?.root_uri) setCurrentUri(selectedScope.root_uri);
              }}
            >
              <FolderOpen className="size-4 shrink-0 text-amber-600" />
              <span className="truncate">{SCOPE_LABELS[scopeName]}</span>
            </button>
            {loading && !treeResponse ? (
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
                </div>
              ) : null}
              {selected && !selected.is_dir && selectedScope?.can_write && (
                <>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    title="删除"
                    onClick={() => void deleteEntry()}
                  >
                    <Trash2 className="size-4 text-destructive" />
                  </Button>
                  <Button
                    size="sm"
                    disabled={!isDirty || saving}
                    onClick={() => void saveContent()}
                  >
                    <Save className="size-4" />
                    保存
                  </Button>
                </>
              )}
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
                readOnly={!selectedScope?.can_write}
                onChange={(event) => setContent(event.target.value)}
                className="h-full min-h-0 resize-none rounded-none border-0 p-4 font-mono text-xs leading-5 focus-visible:ring-0"
              />
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
            <span>
              {(selected?.is_dir
                ? directoryLevels[directoryLevel].length
                : content.length
              ).toLocaleString()}{" "}
              字符
            </span>
          </footer>
        </section>

        <OpenVikingTerminal
          currentUri={currentUri || selectedScope?.root_uri || ""}
          config={config}
          scopeName={scopeName}
          userId={activeUserId}
          onOpenUri={(uri) => {
            const entry = treeResponse?.entries.find((item) => item.uri === uri);
            if (entry) void openEntry(entry);
          }}
          onRefresh={() => void refreshTree()}
        />
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="!max-w-[640px]">
          <DialogHeader>
            <DialogTitle>
              {createType === "directory" ? "新建目录" : "新建文件"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
                当前位置
              </div>
              <code className="block break-all rounded-lg bg-muted p-2 text-xs">
                {currentUri}
              </code>
            </div>
            <Input
              autoFocus
              value={createName}
              onChange={(event) => setCreateName(event.target.value)}
              placeholder={
                createType === "directory"
                  ? "目录名"
                  : mode === "memory"
                    ? "memory-name.md"
                    : "文件名"
              }
              onKeyDown={(event) => {
                if (event.key === "Enter" && createType === "directory") {
                  void createEntry();
                }
              }}
            />
            {createType === "file" && (
              <Textarea
                value={createContent}
                onChange={(event) => setCreateContent(event.target.value)}
                className="min-h-[260px] font-mono text-xs"
                placeholder="文件内容"
              />
            )}
          </div>
          <DialogFooter className="px-0 pb-0">
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button
              disabled={saving || !createName.trim()}
              onClick={() => void createEntry()}
            >
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function TreeRow({
  node,
  depth,
  expanded,
  forceExpanded,
  selectedUri,
  currentUri,
  onOpen,
}: {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  forceExpanded: boolean;
  selectedUri: string;
  currentUri: string;
  onOpen: (entry: WorkspaceEntry) => void;
}) {
  const isExpanded = forceExpanded || expanded.has(node.entry.uri);
  const isCurrentDirectory = node.entry.is_dir && currentUri === node.entry.uri;
  return (
    <>
      <button
        type="button"
        title={node.entry.uri}
        className={cn(
          "group flex w-full items-center gap-1 py-1.5 pr-2 text-left text-xs hover:bg-muted/70",
          selectedUri === node.entry.uri && "bg-muted",
          isCurrentDirectory && "font-semibold text-sidebar-primary",
        )}
        style={{ paddingLeft: `${8 + depth * 14}px` }}
        onClick={() => void onOpen(node.entry)}
      >
        {node.entry.is_dir ? (
          isExpanded ? (
            <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="size-3 shrink-0 text-muted-foreground" />
          )
        ) : (
          <span className="w-3 shrink-0" />
        )}
        {node.entry.is_dir ? (
          isExpanded ? (
            <FolderOpen className="size-4 shrink-0 text-amber-600" />
          ) : (
            <Folder className="size-4 shrink-0 text-amber-600" />
          )
        ) : (
          <FileIcon name={node.entry.name} />
        )}
        <span className="min-w-0 flex-1 truncate">{node.entry.name}</span>
        {!node.entry.is_dir && node.entry.size != null && (
          <span className="hidden shrink-0 text-[9px] text-muted-foreground group-hover:inline">
            {formatBytes(node.entry.size)}
          </span>
        )}
      </button>
      {node.entry.is_dir &&
        isExpanded &&
        node.children.map((child) => (
          <TreeRow
            key={child.entry.uri}
            node={child}
            depth={depth + 1}
            expanded={expanded}
            forceExpanded={forceExpanded}
            selectedUri={selectedUri}
            currentUri={currentUri}
            onOpen={onOpen}
          />
        ))}
    </>
  );
}

function FileIcon({ name }: { name: string }) {
  const extension = fileExtension(name);
  if (JSON_EXTENSIONS.has(extension)) {
    return <FileJson className="size-4 shrink-0 text-amber-600" />;
  }
  if (CODE_EXTENSIONS.has(extension)) {
    return <FileCode2 className="size-4 shrink-0 text-violet-600" />;
  }
  return <FileText className="size-4 shrink-0 text-blue-600" />;
}

function DirectoryLevelPreview({
  level,
  content,
  loading,
}: {
  level: "l0" | "l1";
  content: string;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        正在加载目录 {level.toUpperCase()}…
      </div>
    );
  }
  if (!content) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Empty>
          {level === "l0"
            ? "该目录暂未生成 L0 摘要。"
            : "该目录暂未生成 L1 概览。"}
        </Empty>
      </div>
    );
  }
  return (
    <div className="h-full overflow-auto px-5 py-4">
      <div className="mb-3 flex items-center gap-2">
        <Pill tone={level === "l0" ? "blue" : "purple"}>
          {level.toUpperCase()}
        </Pill>
        <span className="text-xs text-muted-foreground">
          {level === "l0" ? "目录摘要" : "目录概览"}
        </span>
      </div>
      {level === "l1" ? (
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {content}
        </ReactMarkdown>
      ) : (
        <p className="whitespace-pre-wrap text-sm leading-7 text-foreground/90">
          {content}
        </p>
      )}
    </div>
  );
}

function FilePreview({
  entry,
  content,
}: {
  entry: WorkspaceEntry;
  content: string;
}) {
  const extension = fileExtension(entry.name);
  if (MARKDOWN_EXTENSIONS.has(extension)) {
    return (
      <div className="h-full overflow-auto px-5 py-4">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: ({ children }) => (
              <h1 className="mb-4 border-b border-border pb-2 text-2xl font-bold">{children}</h1>
            ),
            h2: ({ children }) => (
              <h2 className="mb-3 mt-6 text-xl font-bold">{children}</h2>
            ),
            h3: ({ children }) => (
              <h3 className="mb-2 mt-5 text-base font-bold">{children}</h3>
            ),
            p: ({ children }) => (
              <p className="my-3 text-sm leading-7 text-foreground/90">{children}</p>
            ),
            ul: ({ children }) => (
              <ul className="my-3 list-disc space-y-1 pl-6 text-sm leading-6">{children}</ul>
            ),
            ol: ({ children }) => (
              <ol className="my-3 list-decimal space-y-1 pl-6 text-sm leading-6">{children}</ol>
            ),
            blockquote: ({ children }) => (
              <blockquote className="my-3 border-l-4 border-sidebar-primary/40 bg-muted/50 px-4 py-2 text-sm text-muted-foreground">
                {children}
              </blockquote>
            ),
            code: ({ children, className }) =>
              className ? (
                <code className={cn("font-mono text-xs", className)}>{children}</code>
              ) : (
                <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{children}</code>
              ),
            pre: ({ children }) => (
              <pre className="my-4 overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-5 text-slate-100">
                {children}
              </pre>
            ),
            table: ({ children }) => (
              <div className="my-4 overflow-auto">
                <table className="w-full border-collapse text-xs">{children}</table>
              </div>
            ),
            th: ({ children }) => (
              <th className="border border-border bg-muted px-2 py-1.5 text-left font-semibold">{children}</th>
            ),
            td: ({ children }) => (
              <td className="border border-border px-2 py-1.5 align-top">{children}</td>
            ),
            a: ({ children, href }) => (
              <a
                className="text-sidebar-primary underline underline-offset-2"
                href={href}
                target="_blank"
                rel="noreferrer"
              >
                {children}
              </a>
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    );
  }
  if (HTML_EXTENSIONS.has(extension)) {
    return (
      <iframe
        title={`Preview ${entry.name}`}
        className="h-full w-full border-0 bg-white"
        sandbox=""
        srcDoc={content}
      />
    );
  }
  if (JSON_EXTENSIONS.has(extension)) {
    return (
      <pre className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-5">
        {formatJsonContent(content)}
      </pre>
    );
  }
  return (
    <pre className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-5">
      {content || "（空文件）"}
    </pre>
  );
}

function OpenVikingTerminal({
  currentUri,
  config,
  scopeName,
  userId,
  onOpenUri,
  onRefresh,
}: {
  currentUri: string;
  config: WorkspaceConfig;
  scopeName: ScopeName;
  userId: string;
  onOpenUri: (uri: string) => void;
  onRefresh: () => void;
}) {
  const [command, setCommand] = useState("");
  const [records, setRecords] = useState<TerminalRecord[]>([]);
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [running, setRunning] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const nextId = useRef(1);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [records]);

  async function run(raw = command) {
    const value = raw.trim();
    if (!value || running || !config.cli_available) return;
    const id = nextId.current++;
    setRecords((previous) => [
      ...previous,
      { id, command: value, running: true },
    ]);
    setHistory((previous) => [
      value,
      ...previous.filter((item) => item !== value),
    ].slice(0, 50));
    setHistoryIndex(-1);
    setCommand("");
    setRunning(true);
    try {
      const result = await api<CliResult>("/api/openviking/workspace/cli", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          scope: scopeName,
          current_uri: currentUri,
          command: value,
        }),
      });
      setRecords((previous) =>
        previous.map((record) =>
          record.id === id ? { ...record, running: false, result } : record,
        ),
      );
      if (result.ok) onRefresh();
    } catch (error: any) {
      setRecords((previous) =>
        previous.map((record) =>
          record.id === id
            ? {
                ...record,
                running: false,
                result: {
                  ok: false,
                  exit_code: -1,
                  command: ["ov"],
                  stdout: "",
                  stderr: error.message || "CLI request failed",
                  truncated: false,
                },
              }
            : record,
        ),
      );
    } finally {
      setRunning(false);
    }
  }

  const quickCommands = [
    { label: "状态", command: "ov status" },
    { label: "当前目录", command: `ov ls ${currentUri}` },
    { label: "目录树", command: `ov tree ${currentUri} -L 2` },
    { label: "全部命令", command: "ov --help" },
  ];

  return (
    <section className="flex min-h-0 flex-col bg-[#fbfbfc]">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-3">
        <div className="flex items-center gap-2">
          <Terminal className="size-4" />
          <span className="text-sm font-semibold">OpenViking CLI</span>
          <Pill tone={config.cli_full_access ? "green" : "gray"}>
            {config.cli_full_access ? "完整权限" : "当前空间"}
          </Pill>
        </div>
        <div className="flex items-center gap-1">
          {config.studio_url && (
            <Button asChild variant="ghost" size="icon-sm" title="打开 Studio">
              <a href={config.studio_url} target="_blank" rel="noreferrer">
                <ExternalLink className="size-4" />
              </a>
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon-sm"
            title="清空终端"
            onClick={() => setRecords([])}
          >
            <Trash2 className="size-4" />
          </Button>
        </div>
      </header>
      <div className="shrink-0 border-b border-border px-3 py-2">
        <div className="truncate font-mono text-[10px] text-muted-foreground">
          scope: {currentUri}
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {quickCommands.map((item) => (
            <button
              key={item.label}
              type="button"
              disabled={running || !config.cli_available}
              className="rounded-md border border-border bg-background px-2 py-1 text-[10px] font-semibold hover:bg-muted disabled:opacity-50"
              onClick={() => void run(item.command)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto p-3">
        {!config.cli_available ? (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
            未找到 OpenViking CLI。请设置 OPENVIKING_CLI_BIN。
          </div>
        ) : records.length === 0 ? (
          <div className="flex h-full min-h-[260px] flex-col items-center justify-center text-center">
            <div className="grid size-14 place-items-center rounded-2xl bg-muted">
              <Terminal className="size-7 text-muted-foreground" />
            </div>
            <div className="mt-4 text-lg font-semibold">OpenViking CLI</div>
            <p className="mt-2 max-w-[280px] text-xs leading-5 text-muted-foreground">
              直接执行原生 ov 命令。支持资源、文件系统、检索、Session、任务、快照与系统管理能力。
            </p>
            <div className="mt-4 grid w-full max-w-[290px] gap-2 text-left">
              {[
                ["ov add-resource URL --wait", "添加资源"],
                ['ov add-memory "需要记住的内容"', "添加记忆"],
                [`ov find "查询词" -u ${currentUri}`, "语义检索"],
              ].map(([code, label]) => (
                <button
                  key={code}
                  type="button"
                  className="rounded-lg border border-border bg-background px-3 py-2 hover:bg-muted"
                  onClick={() => setCommand(code)}
                >
                  <span className="block text-[10px] text-muted-foreground">{label}</span>
                  <code className="mt-1 block truncate text-[10px]">{code}</code>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {records.map((record) => (
              <div key={record.id}>
                <div className="flex items-start gap-2 font-mono text-[11px]">
                  <span className="select-none text-sidebar-primary">❯</span>
                  <span className="break-all">{record.command}</span>
                </div>
                {record.running ? (
                  <div className="mt-2 flex items-center gap-2 pl-4 text-[10px] text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" />
                    正在执行…
                  </div>
                ) : record.result ? (
                  <div
                    className={cn(
                      "mt-2 rounded-lg border p-2.5",
                      record.result.ok
                        ? "border-border bg-background"
                        : "border-destructive/30 bg-destructive/5",
                    )}
                  >
                    <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-4">
                      {record.result.stdout ||
                        record.result.stderr ||
                        `exit ${record.result.exit_code}`}
                    </pre>
                    {record.result.stderr && record.result.stdout && (
                      <pre className="mt-2 border-t border-border pt-2 whitespace-pre-wrap break-words font-mono text-[10px] leading-4 text-destructive">
                        {record.result.stderr}
                      </pre>
                    )}
                    <CliUriLinks
                      text={`${record.result.stdout}\n${record.result.stderr}`}
                      onOpen={onOpenUri}
                    />
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 border-t border-border bg-background p-2.5">
        <div className="flex items-end gap-2 rounded-lg border border-border px-2.5 py-2 focus-within:border-sidebar-primary">
          <span className="pb-0.5 font-mono text-xs text-sidebar-primary">❯</span>
          <textarea
            value={command}
            rows={1}
            disabled={running || !config.cli_available}
            placeholder={'输入 ov 命令，例如 ov find "OpenViking"'}
            className="max-h-24 min-h-5 flex-1 resize-none bg-transparent font-mono text-[11px] leading-5 outline-none placeholder:text-muted-foreground"
            onChange={(event) => setCommand(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void run();
                return;
              }
              if (event.key === "ArrowUp" && history.length) {
                event.preventDefault();
                const next = Math.min(historyIndex + 1, history.length - 1);
                setHistoryIndex(next);
                setCommand(history[next]);
              }
              if (event.key === "ArrowDown" && historyIndex >= 0) {
                event.preventDefault();
                const next = historyIndex - 1;
                setHistoryIndex(next);
                setCommand(next >= 0 ? history[next] : "");
              }
            }}
          />
          <Button
            size="icon-sm"
            disabled={running || !command.trim() || !config.cli_available}
            onClick={() => void run()}
          >
            <Send className="size-3.5" />
          </Button>
        </div>
        <div className="mt-1.5 flex justify-between text-[9px] text-muted-foreground">
          <span>↑↓ 历史命令 · Shift+Enter 换行</span>
          <span>Enter 发送</span>
        </div>
      </div>
    </section>
  );
}

function CliUriLinks({
  text,
  onOpen,
}: {
  text: string;
  onOpen: (uri: string) => void;
}) {
  const uris = Array.from(
    new Set(text.match(/viking:\/\/[^\s,，)）\]}】'"`]+/g) || []),
  ).slice(0, 8);
  if (!uris.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1 border-t border-border pt-2">
      {uris.map((uri) => (
        <button
          key={uri}
          type="button"
          className="max-w-full truncate rounded bg-muted px-2 py-1 font-mono text-[9px] text-sidebar-primary hover:bg-muted/70"
          title={uri}
          onClick={() => onOpen(uri)}
        >
          {uri}
        </button>
      ))}
    </div>
  );
}

function buildTree(entries: WorkspaceEntry[], rootUri: string): TreeNode[] {
  if (!rootUri) return [];
  const nodeMap = new Map<string, TreeNode>();
  for (const entry of entries) {
    nodeMap.set(entry.uri, { entry, children: [] });
  }
  const roots: TreeNode[] = [];
  for (const node of nodeMap.values()) {
    const parent = parentUri(node.entry.uri);
    const parentNode = nodeMap.get(parent);
    if (parentNode && parentNode.entry.is_dir) {
      parentNode.children.push(node);
    } else if (parent === rootUri || node.entry.uri.startsWith(`${rootUri}/`)) {
      roots.push(node);
    }
  }
  const sortNodes = (nodes: TreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.entry.is_dir !== right.entry.is_dir) {
        return left.entry.is_dir ? -1 : 1;
      }
      return left.entry.name.localeCompare(right.entry.name);
    });
    for (const node of nodes) sortNodes(node.children);
  };
  sortNodes(roots);
  return roots;
}

function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  if (!query) return nodes;
  const output: TreeNode[] = [];
  for (const node of nodes) {
    const children = filterTree(node.children, query);
    const haystack = [
      node.entry.name,
      node.entry.uri,
      node.entry.abstract || "",
    ]
      .join(" ")
      .toLowerCase();
    if (haystack.includes(query) || children.length) {
      output.push({ ...node, children });
    }
  }
  return output;
}

function parentUri(uri: string) {
  const clean = uri.replace(/\/+$/, "");
  const schemeEnd = clean.indexOf("://");
  const slash = clean.lastIndexOf("/");
  return slash > schemeEnd + 2 ? clean.slice(0, slash) : clean;
}

function fileExtension(name: string) {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index + 1).toLowerCase() : "";
}

function fileKind(name: string) {
  const extension = fileExtension(name);
  if (MARKDOWN_EXTENSIONS.has(extension)) return "Markdown";
  if (HTML_EXTENSIONS.has(extension)) return "HTML";
  if (JSON_EXTENSIONS.has(extension)) return "JSON";
  if (CODE_EXTENSIONS.has(extension)) return extension.toUpperCase();
  return extension ? extension.toUpperCase() : "Text";
}

function formatJsonContent(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content || "（空文件）";
  }
}

function formatBytes(value?: number | string | null) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "文件";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
