import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronLeft,
  ExternalLink,
  FileText,
  Folder,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { api, type UserProfile, type UsersListResp } from "@/api/client";
import { Empty, Panel, Pill, StatCard } from "@/components/common";
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

type WorkspaceMode = "memory" | "workspace";
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
};

type WorkspaceConfig = {
  enabled: boolean;
  deployment: "cloud" | "local" | string;
  endpoint: string;
  studio_url?: string;
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
};

type ListResponse = {
  scope: ScopeName;
  root_uri: string;
  uri: string;
  entries: WorkspaceEntry[];
  exists: boolean;
  can_write: boolean;
};

const MODE_SCOPES: Record<WorkspaceMode, ScopeName[]> = {
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
  personal_skills: "个人 Skill 空间",
  team_skills: "团队 Skill 空间",
  personal_workspace: "个人 Workspace",
  team_workspace: "团队 Workspace",
};

export default function OpenVikingWorkspaceView({
  active,
  mode,
  user,
}: {
  active: boolean;
  mode: WorkspaceMode;
  user?: UserProfile | null;
}) {
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUserId, setActiveUserId] = useState(user?.id || "");
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const [scopeName, setScopeName] = useState<ScopeName>(MODE_SCOPES[mode][0]);
  const [listing, setListing] = useState<ListResponse | null>(null);
  const [currentUri, setCurrentUri] = useState("");
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null);
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createType, setCreateType] = useState<"file" | "directory">("file");
  const [createName, setCreateName] = useState("");
  const [createContent, setCreateContent] = useState("");

  const scopeNames = MODE_SCOPES[mode];
  const selectedScope = config?.scopes?.[scopeName];
  const isDirty = !!selected && content !== originalContent;
  const filteredEntries = useMemo(() => {
    const query = filter.trim().toLowerCase();
    const entries = listing?.entries || [];
    return query
      ? entries.filter((entry) => entry.name.toLowerCase().includes(query))
      : entries;
  }, [filter, listing]);

  const loadDirectory = useCallback(
    async (uri: string, chosenScope = scopeName, chosenUser = activeUserId) => {
      if (!chosenUser || !uri) return;
      setLoading(true);
      try {
        const result = await api<ListResponse>(
          `/api/openviking/workspace/list?scope=${encodeURIComponent(chosenScope)}&user_id=${encodeURIComponent(chosenUser)}&uri=${encodeURIComponent(uri)}`,
        );
        setListing(result);
        setCurrentUri(result.uri);
        setSelected(null);
        setContent("");
        setOriginalContent("");
      } catch (error: any) {
        toastErr("加载 OpenViking 目录失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [activeUserId, scopeName],
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
        const nextScope = MODE_SCOPES[mode][0];
        setScopeName(nextScope);
        const root = result.scopes?.[nextScope]?.root_uri || "";
        setCurrentUri(root);
        if (result.enabled && root)
          await loadDirectory(root, nextScope, userId);
      } catch (error: any) {
        toastErr("读取 OpenViking 配置失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [loadDirectory, mode],
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
        if (preferred) loadConfig(preferred);
      })
      .catch((error) => toastErr("加载用户失败", error.message));
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const first = MODE_SCOPES[mode][0];
    setScopeName(first);
    const root = config?.scopes?.[first]?.root_uri;
    if (root) loadDirectory(root, first, activeUserId);
  }, [mode]);

  async function chooseScope(next: ScopeName) {
    setScopeName(next);
    const root = config?.scopes?.[next]?.root_uri;
    if (root) await loadDirectory(root, next, activeUserId);
  }

  async function openEntry(entry: WorkspaceEntry) {
    if (entry.is_dir) {
      await loadDirectory(entry.uri);
      return;
    }
    setSelected(entry);
    setLoading(true);
    try {
      const result = await api<{ content: string }>(
        `/api/openviking/workspace/content?scope=${encodeURIComponent(scopeName)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(result.content || "");
      setOriginalContent(result.content || "");
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
      await loadDirectory(currentUri);
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
      await loadDirectory(currentUri);
    } catch (error: any) {
      toastErr("创建失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteEntry() {
    if (!selected || !selectedScope?.can_write) return;
    if (!window.confirm(`确认删除「${selected.name}」？目录会递归删除。`))
      return;
    try {
      await api(
        `/api/openviking/workspace?scope=${encodeURIComponent(scopeName)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(selected.uri)}`,
        { method: "DELETE" },
      );
      toastOk("已从 OpenViking 删除", selected.name);
      await loadDirectory(currentUri);
    } catch (error: any) {
      toastErr("删除失败", error.message);
    }
  }

  const parentUri =
    selectedScope && currentUri !== selectedScope.root_uri
      ? currentUri.slice(0, currentUri.lastIndexOf("/"))
      : "";
  const breadcrumb = selectedScope
    ? breadcrumbItems(selectedScope.root_uri, currentUri)
    : [];

  return (
    <div className="mx-auto max-w-[1320px] px-7 py-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2">
          {scopeNames.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => chooseScope(name)}
              className={cn(
                "rounded-lg border px-3.5 py-2 text-sm font-semibold transition-colors",
                scopeName === name
                  ? "border-sidebar-primary bg-sidebar-primary text-white"
                  : "border-border bg-surface hover:bg-muted",
              )}
            >
              {SCOPE_LABELS[name]}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {users.length > 1 && (
            <select
              value={activeUserId}
              onChange={(event) => {
                setActiveUserId(event.target.value);
                loadConfig(event.target.value);
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
          {config?.studio_url && (
            <Button asChild variant="outline" size="sm">
              <a href={config.studio_url} target="_blank" rel="noreferrer">
                完整 OpenViking Studio <ExternalLink />
              </a>
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={loading || !currentUri}
            onClick={() => loadDirectory(currentUri)}
          >
            <RefreshCw className={loading ? "animate-spin" : ""} /> 刷新
          </Button>
        </div>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3.5">
        <StatCard
          label="部署方式"
          value={
            config?.deployment === "local"
              ? "本地 OpenViking"
              : "云上 OpenViking"
          }
        />
        <StatCard label="当前空间" value={SCOPE_LABELS[scopeName]} />
        <StatCard label="目录项" value={listing?.entries.length || 0} />
        <StatCard
          label="写权限"
          value={selectedScope?.can_write ? "可管理" : "只读"}
        />
      </div>

      {!config?.enabled ? (
        <Panel title="OpenViking 未启用">
          <Empty>
            请先在“运行状态”中启用 OpenViking，并配置本地部署或云端 endpoint。
          </Empty>
        </Panel>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(420px,0.9fr)_minmax(0,1.1fr)]">
          <Panel
            title={mode === "memory" ? "Memory 目录" : "Workspace 文件树"}
            count={listing?.exists === false ? "根目录尚未创建" : currentUri}
            extra={
              selectedScope?.can_write ? (
                <div className="flex gap-1.5">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => startCreate("directory")}
                  >
                    + 目录
                  </Button>
                  <Button size="sm" onClick={() => startCreate("file")}>
                    + 文件
                  </Button>
                </div>
              ) : (
                <Pill tone="gray">只读</Pill>
              )
            }
          >
            <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-3 text-xs">
              {parentUri && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => loadDirectory(parentUri)}
                >
                  <ChevronLeft /> 返回
                </Button>
              )}
              {breadcrumb.map((item, index) => (
                <button
                  key={item.uri}
                  className="rounded px-1.5 py-1 font-mono text-muted-foreground hover:bg-muted hover:text-foreground"
                  onClick={() => loadDirectory(item.uri)}
                >
                  {index ? "/ " : ""}
                  {item.label}
                </button>
              ))}
            </div>
            <div className="border-b border-line p-3">
              <Input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="过滤当前目录"
              />
            </div>
            {!filteredEntries.length ? (
              <Empty>{loading ? "加载中…" : "当前目录为空。"}</Empty>
            ) : (
              <div className="max-h-[520px] overflow-auto">
                {filteredEntries.map((entry) => (
                  <button
                    key={entry.uri}
                    type="button"
                    className={cn(
                      "flex w-full items-start gap-3 border-b border-line px-4 py-3 text-left hover:bg-muted/60",
                      selected?.uri === entry.uri && "bg-muted",
                    )}
                    onClick={() => openEntry(entry)}
                  >
                    {entry.is_dir ? (
                      <Folder className="mt-0.5 size-4 text-amber-600" />
                    ) : (
                      <FileText className="mt-0.5 size-4 text-blue-600" />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">
                        {entry.name}
                      </span>
                      <span className="mt-1 block truncate text-[11px] text-muted-foreground">
                        {entry.is_dir ? "目录" : formatBytes(entry.size)}
                        {entry.abstract ? ` · ${entry.abstract}` : ""}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </Panel>

          <Panel
            title={selected ? selected.name : "内容预览"}
            count={selected?.uri || "选择左侧文件"}
            extra={
              selected && selectedScope?.can_write ? (
                <div className="flex gap-1.5">
                  <Button variant="destructive" size="sm" onClick={deleteEntry}>
                    <Trash2 /> 删除
                  </Button>
                  <Button
                    size="sm"
                    disabled={!isDirty || saving}
                    onClick={saveContent}
                  >
                    <Save /> 保存
                  </Button>
                </div>
              ) : undefined
            }
          >
            {!selected ? (
              <Empty>选择文件查看内容；点击目录可继续下钻。</Empty>
            ) : selected.is_dir ? (
              <Empty>目录已打开。</Empty>
            ) : (
              <Textarea
                value={content}
                readOnly={!selectedScope?.can_write}
                onChange={(event) => setContent(event.target.value)}
                className="min-h-[520px] resize-none rounded-none border-0 font-mono text-xs leading-5 focus-visible:ring-0"
              />
            )}
          </Panel>
        </div>
      )}

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
                if (event.key === "Enter" && createType === "directory")
                  createEntry();
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
              onClick={createEntry}
            >
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function breadcrumbItems(root: string, current: string) {
  const relative = current.slice(root.length).replace(/^\/+/, "");
  const items = [{ label: root, uri: root }];
  if (!relative) return items;
  let uri = root;
  for (const segment of relative.split("/").filter(Boolean)) {
    uri = `${uri}/${segment}`;
    items.push({ label: segment, uri });
  }
  return items;
}

function formatBytes(value?: number | string | null) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "文件";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
