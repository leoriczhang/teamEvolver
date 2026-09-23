import { DiffView, diffStats } from "@/components/WorkspaceDiffView";
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
  GitCompare,
  Loader2,
  Pencil,
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
import SkillTransferModal from "./skills/SkillTransferModal";

export type ScopeName =
  | "personal_memory"
  | "team_memory"
  | "personal_skills"
  | "team_skills"
  | "personal_resources"
  | "team_resources"
  | "personal_workspace"
  | "team_workspace";

export type ScopeConfig = {
  name: ScopeName;
  root_uri: string;
  space: "personal" | "team";
  kind: "memory" | "skills" | "resources" | "workspace";
  can_write: boolean;
  openviking_user?: string;
};

export type WorkspaceConfig = {
  enabled: boolean;
  deployment: "cloud" | "local" | string;
  endpoint: string;
  studio_url?: string;
  cli_available?: boolean;
  cli_full_access?: boolean;
  account?: string;
  root_key_configured?: boolean;
  user_id: string;
  scopes: Record<ScopeName, ScopeConfig>;
};

export type WorkspaceEntry = {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | string | null;
  modified_at?: string;
  abstract?: string;
  relative_path?: string;
};

export type TreeResponse = {
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

type WorkspaceDraft = {
  uri: string;
  name: string;
  scope: ScopeName;
  originalContent: string;
  content: string;
};

type SpaceKey = "personal" | "team";

// A "space" is one user-facing OpenViking namespace.
type SpaceConfig = {
  key: SpaceKey;
  label: string;
  members: { scope: ScopeName; label: string }[];
};

const WORKSPACE_SPACES: SpaceConfig[] = [
  {
    key: "personal",
    label: "个人记忆",
    members: [
      { scope: "personal_workspace", label: "个人记忆" },
    ],
  },
  {
    key: "team",
    label: "团队资源",
    members: [
      { scope: "team_workspace", label: "团队资源" },
    ],
  },
];

const SCOPE_LABELS: Record<ScopeName, string> = {
  personal_memory: "个人 Memory",
  team_memory: "团队 Memory",
  personal_skills: "个人 Skill",
  team_skills: "团队 Skill",
  personal_resources: "个人 Resources",
  team_resources: "团队 Resources",
  personal_workspace: "个人记忆",
  team_workspace: "团队资源",
};

// Synthetic URIs for the per-scope group folders shown at a space's tree root.
const GROUP_URI_PREFIX = "group://";
function groupUri(scope: ScopeName): string {
  return `${GROUP_URI_PREFIX}${scope}`;
}

function initialSpace(): SpaceConfig {
  return WORKSPACE_SPACES[0];
}

const DIRECTORY_PURPOSES: Record<string, string> = {
  skills: "团队正式技能库，Pi Agent / Hermes 读取源",
  "manifest.json": "技能清单索引：名称 → 版本/哈希",
  "evolve_skill_registry.json": "技能 ID 登记表，保证 ID 跨节点稳定",
  skill_lab: "技能实验室：datasets 数据集 / runs 实验结果",
  skill_datasets: "技能测试集，按 <skill>/<dataset> 组织",
  evolution_datasets: "从历史会话合成的进化数据集",
  skill_evidence: "技能效果证据：注入次数、有效性",
  skill_version_context: "技能版本上下文，真回放对比基线",
  sessions: "待消费会话队列，进化引擎消费后删除",
  session_archive: "会话永久归档",
  session_filter_audit: "会话过滤决策审计（为何入队/跳过）",
  session_ledger: "会话总账：queued→consumed 状态流转",
  "session_index.json": "会话元信息索引，供控制台快速浏览",
  skill_mutation_commits: "技能变更提交存档（publish/delete）",
  skill_sync_outbox: "技能同步发件箱，待下发各运行时",
  candidate_skills: "候选技能暂存区，尚未进入正式 skills/",
  validation_jobs: "验证任务，由进化服务产出",
  validation_claims: "任务认领锁，防止重复验证",
  validation_results: "各客户端独立验证结果",
  validation_evaluations: "多方结果聚合评估",
  validation_decisions: "最终发布/拒绝裁决",
  "validation_decision_index.json": "裁决总索引，供快速检索",
  human_review: "人工复核任务队列（自动裁决拿不准时）",
  "memory-changes": "记忆变更总账，支持真回放验证记忆改动",
  "memory-replays": "记忆改动的真回放记录",
  peers: "按客户/用户隔离区，个人技能落在 peers/<账号>/skills",
  knowledge: "OpenViking 顶层数据类别（memories/resources/skills 并列）",
  ".abstract.md": "OpenViking 自动生成的目录 L0 摘要",
  ".overview.md": "OpenViking 自动生成的目录 L1 概览",
};

function directoryPurpose(entry: WorkspaceEntry): string {
  return DIRECTORY_PURPOSES[entry.name] || "";
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
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUserId, setActiveUserId] = useState(
    () => window.localStorage.getItem("teamEvolver.activeUserId") || user?.id || "",
  );
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const spaces = WORKSPACE_SPACES;
  const [activeSpaceKey, setActiveSpaceKey] = useState<SpaceKey>(spaces[0].key);
  // Merged entries across a space's member scopes; each entry keeps its full
  // URI so the owning scope can be resolved for per-file operations.
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [currentUri, setCurrentUri] = useState("");
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null);
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [viewMode, setViewMode] = useState<"preview" | "source" | "diff">("preview");
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
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, WorkspaceDraft>>({});
  const [reviewOpen, setReviewOpen] = useState(false);
  const [transfer, setTransfer] = useState<"import" | "export" | null>(null);
  const spaceLoadIdRef = useRef(0);
  const configLoadIdRef = useRef(0);

  const activeSpace = spaces.find((item) => item.key === activeSpaceKey) || spaces[0];
  const spaceScopes = useMemo(
    () => activeSpace.members
      .map((member) => config?.scopes?.[member.scope])
      .filter((scope): scope is ScopeConfig => !!scope),
    [activeSpace, config],
  );
  // Resolve which member scope owns a given URI (longest matching root wins).
  const scopeForUri = useCallback(
    (uri: string): ScopeConfig | undefined => {
      const clean = (uri || "").replace(/\/+$/, "");
      let best: ScopeConfig | undefined;
      for (const scope of spaceScopes) {
        const root = scope.root_uri.replace(/\/+$/, "");
        if ((clean === root || clean.startsWith(`${root}/`)) &&
          (!best || scope.root_uri.length > best.root_uri.length)) {
          best = scope;
        }
      }
      return best;
    },
    [spaceScopes],
  );
  const selectedScope = selected ? scopeForUri(selected.uri) : undefined;
  const draftList = useMemo(
    () => Object.values(drafts).sort((left, right) => left.uri.localeCompare(right.uri)),
    [drafts],
  );
  const dirtyUris = useMemo(() => new Set(draftList.map((draft) => draft.uri)), [draftList]);
  const isDirty = !!selected && dirtyUris.has(selected.uri);
  const canEditSelected = !!(
    editing &&
    selectedScope?.can_write &&
    (selectedScope.kind === "memory" || selectedScope.kind === "skills" || selectedScope.kind === "workspace")
  );
  const hasEditableScopes = !!config?.enabled && spaceScopes.some(
    (scope) => scope.can_write && (scope.kind === "memory" || scope.kind === "skills" || scope.kind === "workspace"),
  );
    const currentScope = scopeForUri(currentUri);
    const activeWritableScope =
      currentScope?.can_write
        ? currentScope
        : spaceScopes.find((scope) => scope.can_write) || spaceScopes[0];
    const terminalScope =
      currentScope?.name ||
      selectedScope?.name ||
      activeWritableScope?.name ||
      activeSpace.members[0]?.scope;
  const tree = useMemo(
    () => buildSpaceTree(activeSpace, spaceScopes, entries),
    [activeSpace, spaceScopes, entries],
  );
  const filteredTree = useMemo(
    () => filterTree(tree, filter.trim().toLowerCase()),
    [filter, tree],
  );

  const loadSpace = useCallback(
    async (
      space: SpaceConfig,
      chosenUser: string,
      cfg: WorkspaceConfig,
      resetSelection = true,
    ) => {
      if (!chosenUser) return;
      const members = space.members
        .map((member) => cfg.scopes?.[member.scope])
        .filter((scope): scope is ScopeConfig => !!scope);
      if (!members.length) return;
      const loadId = ++spaceLoadIdRef.current;
      setLoading(true);
      try {
        const results = await Promise.all(
          members.map(async (scope) => {
            try {
              const result = await api<TreeResponse>(
                `/api/openviking/workspace/tree?scope=${encodeURIComponent(scope.name)}&user_id=${encodeURIComponent(chosenUser)}&uri=${encodeURIComponent(scope.root_uri)}`,
              );
              return result.entries || [];
            } catch {
              return [] as WorkspaceEntry[];
            }
          }),
        );
        if (loadId !== spaceLoadIdRef.current) return;
        const merged = results.flat();
        setEntries(merged);
        if (resetSelection) {
          setCurrentUri(members[0].root_uri);
          setSelected(null);
          setContent("");
          setOriginalContent("");
          setViewMode("preview");
          setDirectoryLevel("l0");
          setDirectoryLevels({ l0: "", l1: "" });
          setFilter("");
        }
        setExpanded(new Set(members.map((scope) => groupUri(scope.name))));
      } catch (error: any) {
        if (loadId === spaceLoadIdRef.current) {
          toastErr("加载 OpenViking 文件树失败", error.message);
        }
      } finally {
        if (loadId === spaceLoadIdRef.current) {
          setLoading(false);
        }
      }
    },
    [],
  );

  const loadConfig = useCallback(
    async (userId: string) => {
      if (!userId) return;
      const loadId = ++configLoadIdRef.current;
      setLoading(true);
      try {
        const result = await api<WorkspaceConfig>(
          `/api/openviking/workspace/config?user_id=${encodeURIComponent(userId)}`,
        );
        if (loadId !== configLoadIdRef.current) return;
        setConfig(result);
        const first = initialSpace();
        setActiveSpaceKey(first.key);
        if (result.enabled) {
          await loadSpace(first, userId, result);
        } else {
          setLoading(false);
        }
      } catch (error: any) {
        if (loadId === configLoadIdRef.current) {
          setLoading(false);
          toastErr("读取 OpenViking 配置失败", error.message);
        }
      }
    },
    [loadSpace],
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
    if (!draftList.length) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [draftList.length]);

  async function chooseSpace(next: SpaceConfig) {
    setActiveSpaceKey(next.key);
    if (config?.enabled) await loadSpace(next, activeUserId, config);
  }

  async function refreshTree(resetSelection = false) {
    if (config?.enabled) await loadSpace(activeSpace, activeUserId, config, resetSelection);
  }

  async function openEntry(entry: WorkspaceEntry) {
    // Synthetic group folders only toggle expansion; no backing URI to read.
    if (entry.uri.startsWith(GROUP_URI_PREFIX)) {
      setExpanded((previous) => {
        const next = new Set(previous);
        if (next.has(entry.uri)) next.delete(entry.uri);
        else next.add(entry.uri);
        return next;
      });
      return;
    }
    const owner = scopeForUri(entry.uri);
    if (!owner) return;
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
          `/api/openviking/workspace/level?scope=${encodeURIComponent(owner.name)}` +
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
    const draft = drafts[entry.uri];
    if (draft) {
      setContent(draft.content);
      setOriginalContent(draft.originalContent);
      setViewMode("source");
      setDirectoryLevels({ l0: "", l1: "" });
      return;
    }
    setLoading(true);
    try {
      const result = await api<{ content: string }>(
        `/api/openviking/workspace/content?scope=${encodeURIComponent(owner.name)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(result.content || "");
      setOriginalContent(result.content || "");
      setViewMode(
        editing && owner.can_write && (owner.kind === "memory" || owner.kind === "skills")
          ? "source"
          : "preview",
      );
      setDirectoryLevels({ l0: "", l1: "" });
    } catch (error: any) {
      toastErr("读取文件失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  function updateDraftContent(nextContent: string) {
    setContent(nextContent);
    if (!selected || selected.is_dir || !selectedScope || !canEditSelected) return;
    setDrafts((previous) => {
      const next = { ...previous };
      if (nextContent === originalContent) {
        delete next[selected.uri];
      } else {
        next[selected.uri] = {
          uri: selected.uri,
          name: selected.name,
          scope: selectedScope.name,
          originalContent,
          content: nextContent,
        };
      }
      return next;
    });
  }

  function beginEditing() {
    if (!hasEditableScopes) return;
    setEditing(true);
    if (
      selected &&
      !selected.is_dir &&
      selectedScope?.can_write &&
      (selectedScope.kind === "memory" || selectedScope.kind === "skills")
    ) {
      setViewMode("source");
    }
  }

  function discardEditing(requireConfirmation = true) {
    if (
      requireConfirmation &&
      draftList.length &&
      !window.confirm("确认放弃 " + draftList.length + " 个文件的全部未保存改动？")
    ) {
      return false;
    }
    const selectedDraft = selected ? drafts[selected.uri] : undefined;
    if (selectedDraft) {
      setContent(selectedDraft.originalContent);
      setOriginalContent(selectedDraft.originalContent);
    }
    setDrafts({});
    setEditing(false);
    setReviewOpen(false);
    setViewMode("preview");
    return true;
  }

  function finishEditing() {
    if (!draftList.length) {
      discardEditing(false);
      return;
    }
    setReviewOpen(true);
  }

  async function saveDrafts() {
    if (!draftList.length) return;
    setSaving(true);
    try {
      await api<{ saved: boolean; changed_count: number }>(
        "/api/openviking/workspace/batch-content",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: activeUserId,
            changes: draftList.map((draft) => ({
              scope: draft.scope,
              uri: draft.uri,
              original_content: draft.originalContent,
              content: draft.content,
            })),
          }),
        },
      );
      const selectedDraft = selected ? drafts[selected.uri] : undefined;
      if (selectedDraft) setOriginalContent(selectedDraft.content);
      setDrafts({});
      setEditing(false);
      setReviewOpen(false);
      setViewMode("preview");
      toastOk("工作区改动已保存", "共 " + draftList.length + " 个文件");
      await refreshTree();
    } catch (error: any) {
      toastErr("批量保存失败，草稿已保留", error.message);
    } finally {
      setSaving(false);
    }
  }

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

function TreeRow({
  node,
  depth,
  expanded,
  forceExpanded,
  selectedUri,
  currentUri,
  dirtyUris,
  onOpen,
}: {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  forceExpanded: boolean;
  selectedUri: string;
  currentUri: string;
  dirtyUris: Set<string>;
  onOpen: (entry: WorkspaceEntry) => void;
}) {
  const isExpanded = forceExpanded || expanded.has(node.entry.uri);
  const isCurrentDirectory = node.entry.is_dir && currentUri === node.entry.uri;
  // Known top-level entries carry an inline Chinese purpose annotation.
  const purpose = depth === 1 ? directoryPurpose(node.entry) : "";
  return (
    <>
      <button
        type="button"
        title={purpose ? `${node.entry.uri}\n${purpose}` : node.entry.uri}
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
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate">{node.entry.name}</span>
          {purpose && (
            <span className="truncate text-[10px] font-normal text-muted-foreground">
              {purpose}
            </span>
          )}
        </span>
        {dirtyUris.has(node.entry.uri) && (
          <span
            className="size-2 shrink-0 rounded-full bg-amber-500"
            title="该文件有未保存改动"
          />
        )}
        {!node.entry.is_dir && node.entry.size != null && (
          <span className="hidden shrink-0 self-center text-[9px] text-muted-foreground group-hover:inline">
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
            dirtyUris={dirtyUris}
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

// Build a space's tree: one synthetic group folder per member scope, each
// containing that scope's real subtree. A single-member space (platform)
// skips the wrapper and shows the entries directly.
function buildSpaceTree(
  space: SpaceConfig,
  scopes: ScopeConfig[],
  entries: WorkspaceEntry[],
): TreeNode[] {
  if (scopes.length <= 1) {
    const only = scopes[0];
    return only ? buildTree(entries, only.root_uri) : [];
  }
  const groups: TreeNode[] = [];
  for (const member of space.members) {
    const scope = scopes.find((item) => item.name === member.scope);
    if (!scope) continue;
    const children = buildTree(
      entries.filter((entry) => {
        const root = scope.root_uri.replace(/\/+$/, "");
        const uri = entry.uri.replace(/\/+$/, "");
        return uri === root || uri.startsWith(`${root}/`);
      }),
      scope.root_uri,
    );
    groups.push({
      entry: {
        uri: groupUri(scope.name),
        name: member.label,
        is_dir: true,
      },
      children,
    });
  }
  return groups;
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
