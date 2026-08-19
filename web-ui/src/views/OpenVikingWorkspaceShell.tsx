import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  Code2,
  ExternalLink,
  Eye,
  FileCode2,
  FileJson,
  FileText,
  FlaskConical,
  Folder,
  FolderTree,
  FolderOpen,
  GitCompare,
  Beaker,
  Brain,
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

import { api, type MemoryReplayBranch, type MemoryTrueReplay, type UserProfile, type UsersListResp } from "@/api/client";
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
import SkillLabView from "@/views/SkillLabView";

type WorkspaceMode = "workspace" | "platform";
export type ScopeName =
  | "personal_memory"
  | "team_memory"
  | "personal_skills"
  | "team_skills"
  | "personal_resources"
  | "team_resources"
  | "personal_workspace"
  | "team_workspace"
  | "platform_assets";

export type ScopeConfig = {
  name: ScopeName;
  root_uri: string;
  space: "personal" | "team";
  kind: "memory" | "skills" | "resources" | "workspace" | "platform";
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
  personal_access_configured?: boolean;
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

type SpaceKey = "personal" | "team" | "platform";

// A "space" is what the console shows as one workspace tab. Agent-referable
// assets are split across physical roots (skills vs memories vs the user
// root), so each space aggregates its member scopes and renders them under
// labeled group folders. Platform storage is a single read-only scope.
type SpaceConfig = {
  key: SpaceKey;
  label: string;
  // Member scopes shown as top-level group folders, in display order.
  members: { scope: ScopeName; label: string }[];
};

const WORKSPACE_SPACES: SpaceConfig[] = [
  {
    key: "personal",
    label: "个人 Workspace",
    members: [
      { scope: "personal_skills", label: "技能 Skills" },
      { scope: "personal_memory", label: "记忆 Memory" },
      { scope: "personal_resources", label: "资源 Resources" },
    ],
  },
  {
    key: "team",
    label: "团队 Workspace",
    members: [
      { scope: "team_skills", label: "技能 Skills" },
      { scope: "team_memory", label: "记忆 Memory" },
      { scope: "team_resources", label: "资源 Resources" },
    ],
  },
];

const PLATFORM_SPACE: SpaceConfig = {
  key: "platform",
  label: "平台内部存储",
  members: [{ scope: "platform_assets", label: "平台资产" }],
};

// Platform-internal directories under the team resources root. Agent-referable
// directories (skills / peers / knowledge) are intentionally excluded — those
// belong to the Agent workspace, not the platform assets view.
const PLATFORM_DIR_ALLOWLIST = new Set<string>([
  "skill_lab",
  "skill_datasets",
  "evolution_datasets",
  "skill_evidence",
  "skill_version_context",
  "sessions",
  "session_archive",
  "session_filter_audit",
  "session_ledger",
  "session_index.json",
  "skill_mutation_commits",
  "skill_sync_outbox",
  "candidate_skills",
  "validation_jobs",
  "validation_claims",
  "validation_results",
  "validation_evaluations",
  "validation_decisions",
  "validation_decision_index.json",
  "human_review",
  "memory-changes",
  "memory-replays",
  "manifest.json",
  "evolve_skill_registry.json",
]);

function spacesForMode(mode: WorkspaceMode): SpaceConfig[] {
  return mode === "platform" ? [PLATFORM_SPACE] : WORKSPACE_SPACES;
}

const SCOPE_LABELS: Record<ScopeName, string> = {
  personal_memory: "个人 Memory",
  team_memory: "团队 Memory",
  personal_skills: "个人 Skill",
  team_skills: "团队 Skill",
  personal_resources: "个人 Resources",
  team_resources: "团队 Resources",
  personal_workspace: "个人 Workspace",
  team_workspace: "团队 Workspace",
  platform_assets: "平台资产",
};

function isPersonalSpace(space: SpaceKey) {
  return space === "personal";
}

// Synthetic URIs for the per-scope group folders shown at a space's tree root.
const GROUP_URI_PREFIX = "group://";
function groupUri(scope: ScopeName): string {
  return `${GROUP_URI_PREFIX}${scope}`;
}

function initialSpace(mode: WorkspaceMode, config?: WorkspaceConfig | null): SpaceConfig {
  const list = spacesForMode(mode);
  // If personal credentials/roots are unavailable, open the team space first.
  if (mode === "workspace" && config?.personal_access_configured === false) {
    return list.find((space) => space.key === "team") || list[0];
  }
  return list[0];
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
  mode,
  user,
  labs,
}: {
  active: boolean;
  mode: WorkspaceMode;
  user?: UserProfile | null;
  labs?: {
    skill: ReactNode;
    memory: ReactNode;
  };
}) {
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUserId, setActiveUserId] = useState(
    () => window.localStorage.getItem("teamEvolver.activeUserId") || user?.id || "",
  );
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const [surface, setSurface] = useState<"workspace" | "skill-lab" | "memory-lab">("workspace");
  const spaces = spacesForMode(mode);
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
  const [createOpen, setCreateOpen] = useState(false);
  const [createType, setCreateType] = useState<"file" | "directory">("file");
  const [createName, setCreateName] = useState("");
  const [createContent, setCreateContent] = useState("");

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
  const isDirty = !!selected && !selected.is_dir && content !== originalContent;
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
  // Which experiment the current selection supports: skills → True Replay,
  // memory → injection comparison. Derived from the owning scope + file.
  const evalKind: "skills" | "memory" | null = selectedScope?.kind === "skills"
    ? "skills"
    : selectedScope?.kind === "memory"
      ? "memory"
      : null;
  const evalSkillName = useMemo(
    () => (evalKind === "skills" && selectedScope
      ? skillNameFromUri(selectedScope.root_uri, selected?.uri || currentUri)
      : ""),
    [evalKind, selectedScope, selected?.uri, currentUri],
  );
  const [evalOpen, setEvalOpen] = useState(false);
  // Build one synthetic group-folder per member scope; real entries hang under
  // it, so a space renders as a single tree with labeled asset groups.
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
      setLoading(true);
      try {
        const results = await Promise.all(
          members.map(async (scope) => {
            try {
              const result = await api<TreeResponse>(
                `/api/openviking/workspace/tree?scope=${encodeURIComponent(scope.name)}&user_id=${encodeURIComponent(chosenUser)}&uri=${encodeURIComponent(scope.root_uri)}`,
              );
              let list = result.entries || [];
              // Platform view: only surface platform-internal directories.
              if (scope.kind === "platform") {
                const root = scope.root_uri.replace(/\/+$/, "");
                list = list.filter((entry) => {
                  const rel = entry.uri.replace(/\/+$/, "").slice(root.length + 1);
                  const top = rel.split("/")[0] || entry.name;
                  return PLATFORM_DIR_ALLOWLIST.has(top);
                });
              }
              return list;
            } catch {
              return [] as WorkspaceEntry[];
            }
          }),
        );
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
        const first = initialSpace(mode, result);
        setActiveSpaceKey(first.key);
        if (result.enabled) {
          await loadSpace(first, userId, result);
        }
      } catch (error: any) {
        toastErr("读取 OpenViking 配置失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [loadSpace, mode],
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
    const first = initialSpace(mode, config);
    setActiveSpaceKey(first.key);
    void loadSpace(first, activeUserId, config);
  }, [mode]);

  async function chooseSpace(next: SpaceConfig) {
    setSurface("workspace");
    setActiveSpaceKey(next.key);
    if (config) await loadSpace(next, activeUserId, config);
  }

  async function refreshTree(resetSelection = false) {
    if (config) await loadSpace(activeSpace, activeUserId, config, resetSelection);
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
    setLoading(true);
    try {
      const result = await api<{ content: string }>(
        `/api/openviking/workspace/content?scope=${encodeURIComponent(owner.name)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(entry.uri)}`,
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
          scope: selectedScope.name,
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
      type === "file" && selectedScope?.kind === "memory" ? "# 新记忆\n\n" : "",
    );
    setCreateOpen(true);
  }

  async function createEntry() {
    const name = createName.trim().replace(/^\/+|\/+$/g, "");
    if (!name) {
      toastErr("请输入名称");
      return;
    }
    const owner = scopeForUri(currentUri);
    if (!owner) {
      toastErr("请先在某个资产分组内选择位置");
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
            scope: owner.name,
            uri,
          }),
        });
      } else {
        await api("/api/openviking/workspace/content", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: activeUserId,
            scope: owner.name,
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
        `/api/openviking/workspace?scope=${encodeURIComponent(selectedScope.name)}&user_id=${encodeURIComponent(activeUserId)}&uri=${encodeURIComponent(selected.uri)}`,
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
            {spaces.map((space) => {
            const unavailable =
              mode === "workspace" &&
              config.personal_access_configured === false && isPersonalSpace(space.key);
            return (
              <button
                  key={space.key}
                type="button"
                disabled={unavailable}
                title={unavailable ? "请先在用户管理中配置个人 OpenViking 凭证" : undefined}
                  onClick={() => void chooseSpace(space)}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors",
                    activeSpaceKey === space.key
                    ? "bg-sidebar-primary text-white"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  unavailable && "cursor-not-allowed opacity-45 hover:bg-transparent hover:text-muted-foreground",
                )}
              >
                  {space.label}
                {unavailable ? "（需个人凭证）" : ""}
              </button>
            );
          })}
          <span className="mx-1 h-5 w-px bg-border" />
          {mode === "platform" ? (
            <Pill tone="gray">平台内部 · 只读</Pill>
          ) : (
              <Pill tone={selectedScope?.can_write ? "green" : "gray"}>
                {selected && !selected.is_dir ? (selectedScope?.can_write ? "可管理" : "只读") : "选择资产分组内的文件"}
            </Pill>
          )}
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {selected?.uri || currentUri || activeSpace.label}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {mode === "workspace" && labs && (
            <div
              className="mr-1 flex rounded-lg border border-border bg-muted p-0.5"
              role="tablist"
              aria-label="Agent 工作空间视图"
            >
              {[
                { key: "workspace" as const, label: "Workspace", icon: FolderTree },
                { key: "skill-lab" as const, label: "Skill Lab", icon: Beaker },
                { key: "memory-lab" as const, label: "Memory Lab", icon: Brain },
              ].map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={surface === key}
                  title={label}
                  onClick={() => setSurface(key)}
                  className={cn(
                    "flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[11px] font-semibold transition-colors",
                    surface === key
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <Icon className="size-3.5" />
                  {label}
                </button>
              ))}
            </div>
          )}
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

      {surface === "skill-lab" && labs ? (
        <div className="min-h-0">{labs.skill}</div>
      ) : surface === "memory-lab" && labs ? (
        <div className="min-h-0">{labs.memory}</div>
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
              {currentScope?.can_write && (
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
                  <button
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
                  </button>
                </div>
              ) : null}
              {selected && !selected.is_dir && evalKind === "skills" && evalSkillName && (
                <Button
                  variant="outline"
                  size="sm"
                  title={`用数据集对 ${evalSkillName} 跑 True Replay（Baseline=已存版本，Candidate=当前草稿）`}
                  onClick={() => setEvalOpen(true)}
                >
                  <FlaskConical className="size-4" />
                  True Replay
                </Button>
              )}
              {selected && !selected.is_dir && evalKind === "memory" && (
                <Button
                  variant="outline"
                  size="sm"
                  title="对比改动前后 Agent 召回/注入的记忆上下文"
                  onClick={() => setEvalOpen(true)}
                >
                  <FlaskConical className="size-4" />
                  注入对比
                </Button>
              )}
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
                  : selectedScope?.kind === "memory"
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

      <Dialog open={evalOpen} onOpenChange={setEvalOpen}>
        <DialogContent className="flex h-[92vh] w-full !max-w-[1400px] flex-col overflow-hidden p-0">
          <DialogHeader className="border-b border-line px-5 py-3">
            <DialogTitle className="flex items-center gap-2">
              <FlaskConical className="size-4" />
              {evalKind === "skills"
                ? `Skill 实验评估 · ${evalSkillName}`
                : "Memory 注入对比"}
            </DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            {evalKind === "skills" && evalSkillName ? (
              <SkillLabView
                active={evalOpen}
                user={user}
                embedded
                lockedSkillName={evalSkillName}
                externalDraftMd={content}
              />
              ) : evalKind === "memory" && selectedScope ? (
              <MemoryInjectionCompare
                userId={activeUserId}
                  scopeName={selectedScope.name}
                  scopeLabel={SCOPE_LABELS[selectedScope.name]}
                fileName={selected?.name || ""}
                memoryUri={selected?.uri || ""}
                originalContent={originalContent}
                draftContent={content}
                isDirty={isDirty}
              />
            ) : null}
          </div>
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

// A skill lives at <skills_root>/<skill_name>/... — take the first path
// segment under the scope root as the skill name for True Replay.
function skillNameFromUri(rootUri: string, uri: string): string {
  const root = rootUri.replace(/\/+$/, "");
  const target = (uri || "").replace(/\/+$/, "");
  if (!root || !target.startsWith(`${root}/`)) return "";
  const rest = target.slice(root.length + 1);
  const segment = rest.split("/")[0] || "";
  return segment.toLowerCase() === "skill.md" ? "" : segment;
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

type DiffLine = {
  type: "equal" | "add" | "del";
  text: string;
  oldNumber?: number;
  newNumber?: number;
};

// Line-level LCS diff so the editor can show git-style colored changes
// between the stored file (originalContent) and the working draft (content).
function computeLineDiff(original: string, next: string): DiffLine[] {
  const a = original.length ? original.split("\n") : [];
  const b = next.length ? next.split("\n") : [];
  const rows = a.length;
  const cols = b.length;
  const lcs: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  );
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const diff: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let oldNumber = 1;
  let newNumber = 1;
  while (i < rows && j < cols) {
    if (a[i] === b[j]) {
      diff.push({ type: "equal", text: a[i], oldNumber: oldNumber++, newNumber: newNumber++ });
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      diff.push({ type: "del", text: a[i], oldNumber: oldNumber++ });
      i += 1;
    } else {
      diff.push({ type: "add", text: b[j], newNumber: newNumber++ });
      j += 1;
    }
  }
  while (i < rows) diff.push({ type: "del", text: a[i++], oldNumber: oldNumber++ });
  while (j < cols) diff.push({ type: "add", text: b[j++], newNumber: newNumber++ });
  return diff;
}

function DiffView({ original, next }: { original: string; next: string }) {
  const lines = useMemo(() => computeLineDiff(original, next), [original, next]);
  const added = lines.filter((line) => line.type === "add").length;
  const removed = lines.filter((line) => line.type === "del").length;
  if (!added && !removed) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Empty>草稿与已保存版本一致，暂无改动。</Empty>
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b border-border bg-surface-subtle px-4 py-2 text-[11px] font-semibold">
        <span className="text-[#1a7f37]">+{added} 新增</span>
        <span className="text-[#cf222e]">-{removed} 删除</span>
        <span className="text-muted-foreground">与已保存版本对比</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto font-mono text-[11px] leading-5">
        {lines.map((line, index) => (
          <div
            key={index}
            className={cn(
              "flex border-l-2 border-transparent whitespace-pre-wrap break-words",
              line.type === "add" && "border-[#2da44e] bg-[#e6ffec]",
              line.type === "del" && "border-[#cf222e] bg-[#ffebe9]",
            )}
          >
            <span className="w-10 shrink-0 select-none border-r border-border/60 px-1 text-right text-muted-soft">
              {line.oldNumber ?? ""}
            </span>
            <span className="w-10 shrink-0 select-none border-r border-border/60 px-1 text-right text-muted-soft">
              {line.newNumber ?? ""}
            </span>
            <span
              className={cn(
                "w-4 shrink-0 select-none text-center",
                line.type === "add" && "font-semibold text-[#1a7f37]",
                line.type === "del" && "font-semibold text-[#cf222e]",
                line.type === "equal" && "text-muted-soft",
              )}
            >
              {line.type === "add" ? "+" : line.type === "del" ? "-" : ""}
            </span>
            <span className="flex-1 px-2">{line.text || "\u00a0"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

type MemoryDebugItem = {
  scope: string;
  space: string;
  title: string;
  path_alias: string;
  l0?: string;
  l1?: string;
  score?: number;
};

type MemoryDebugResult = {
  items: MemoryDebugItem[];
  agent_context: string;
  budget: {
    used_items: number;
    used_chars: number;
    max_items: number;
    max_chars: number;
    truncated: boolean;
  };
};

// Memory experiment panel with two modes:
//  - 注入对比: preview what Agent Context retrieves/injects for a query.
//  - 真回放 A/B: run the Agent twice (stored memory vs draft) and compare
//    turns / tool calls / tokens, same engine as Skill True Replay.
type MemoryReplayResult = MemoryTrueReplay;

export function MemoryInjectionCompare({
  userId,
  scopeName,
  scopeLabel,
  fileName,
  memoryUri,
  originalContent,
  draftContent,
  isDirty,
}: {
  userId: string;
  scopeName: ScopeName;
  scopeLabel: string;
  fileName: string;
  memoryUri: string;
  originalContent: string;
  draftContent: string;
  isDirty: boolean;
}) {
  const [tab, setTab] = useState<"inject" | "replay">("inject");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<MemoryDebugResult | null>(null);
  const [loading, setLoading] = useState(false);

  const [replayChecklist, setReplayChecklist] = useState("");
  const [replaySessionId, setReplaySessionId] = useState("");
  const [replayResult, setReplayResult] = useState<MemoryReplayResult | null>(null);
  const [replaying, setReplaying] = useState(false);

  async function runDebug() {
    if (!query.trim() || !userId) return;
    setLoading(true);
    try {
      setResult(
        await api<MemoryDebugResult>("/api/openviking/memory/debug", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: userId,
            query,
            max_items: 12,
            max_chars: 16000,
          }),
        }),
      );
    } catch (error: any) {
      toastErr("Memory 注入调试失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  async function runReplay() {
    if (!query.trim()) {
      toastErr("请先填写 Agent Query");
      return;
    }
    if (!replayChecklist.trim()) {
      toastErr("请至少填写一条 Checklist");
      return;
    }
    setReplaying(true);
    setReplayResult(null);
    try {
      const replay = await api<MemoryReplayResult>(
        "/api/openviking/memory/true-replay",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            memory_path: memoryUri,
            scope: scopeName,
            before_content: originalContent,
            after_content: draftContent,
            query,
            checklist: replayChecklist,
            ...(replaySessionId.trim()
              ? { source_session_id: replaySessionId.trim() }
              : {}),
          }),
        },
      );
      setReplayResult(replay);
      toastOk("Memory 真回放完成", replay.replay_id || "");
    } catch (error: any) {
      toastErr("Memory 真回放失败", error.message);
    } finally {
      setReplaying(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex rounded-lg bg-muted p-0.5 text-xs font-semibold">
        <button
          type="button"
          className={cn(
            "flex-1 rounded-md px-3 py-1.5",
            tab === "inject" ? "bg-background shadow-sm" : "text-muted-foreground",
          )}
          onClick={() => setTab("inject")}
        >
          注入对比（快速预览）
        </button>
        <button
          type="button"
          className={cn(
            "flex-1 rounded-md px-3 py-1.5",
            tab === "replay" ? "bg-background shadow-sm" : "text-muted-foreground",
          )}
          onClick={() => setTab("replay")}
        >
          真回放 A/B（深度验证）
        </button>
      </div>

      <section className="rounded-lg border border-border bg-surface">
        <div className="border-b border-line px-4 py-2.5 text-sm font-semibold">
          记忆改动差异 · {scopeLabel} / {fileName || "未选择"}
        </div>
        <div className="h-[220px]">
          <DiffView original={originalContent} next={draftContent} />
        </div>
      </section>

      {tab === "inject" ? (
        <section className="rounded-lg border border-border bg-surface">
          <div className="border-b border-line bg-amber-500/5 px-4 py-2 text-[11px] leading-relaxed text-amber-700">
            注入对比读取当前已保存的记忆，展示某 Query 会召回并注入哪些上下文。
            {isDirty && " 草稿尚未保存，预览基于已保存版本；如需验证草稿改动请用「真回放 A/B」。"}
          </div>
          <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
            <label className="min-w-[320px] flex-1 text-xs font-semibold text-muted-foreground">
              Agent Query
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => event.key === "Enter" && void runDebug()}
                className="mt-1"
                placeholder="输入 Agent 当前任务，查看会召回并注入哪些个人 / 团队 Memory"
              />
            </label>
            <Button onClick={() => void runDebug()} disabled={loading || !query.trim()}>
              <Search className="size-4" />
              {loading ? "检索中…" : "模拟注入"}
            </Button>
          </div>
          {result ? (
            <div className="grid gap-4 p-4 lg:grid-cols-[1fr_1.1fr]">
              <div>
                <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                  检索命中
                  <Pill tone="gray">
                    {result.budget.used_items} 条 · {result.budget.used_chars} 字符
                  </Pill>
                </div>
                <div className="max-h-[420px] space-y-2 overflow-auto">
                  {result.items.length ? (
                    result.items.map((item) => (
                      <div
                        key={`${item.scope}-${item.path_alias}`}
                        className="rounded-md border border-border p-3"
                      >
                        <div className="flex items-center gap-2">
                          <Pill tone={item.space === "personal" ? "blue" : "purple"}>
                            {item.space === "personal" ? "个人" : "团队"}
                          </Pill>
                          <strong className="text-xs">{item.title}</strong>
                          {item.score != null && (
                            <span className="ml-auto text-[10px] text-muted-foreground">
                              score {item.score}
                            </span>
                          )}
                        </div>
                        <p className="mt-1.5 whitespace-pre-wrap text-[11px] leading-6 text-muted-foreground">
                          {item.l0 || item.l1 || "无摘要"}
                        </p>
                      </div>
                    ))
                  ) : (
                    <Empty>该 Query 未命中任何 Memory。</Empty>
                  )}
                </div>
              </div>
              <div>
                <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                  实际注入 Agent 的上下文
                  <Pill tone={result.budget.truncated ? "amber" : "green"}>
                    {result.budget.truncated ? "已截断" : "预算内"}
                  </Pill>
                </div>
                <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-3 font-mono text-[11px] leading-6">
                  {result.agent_context || "（无注入内容）"}
                </pre>
              </div>
            </div>
          ) : (
            <div className="p-4">
              <Empty>输入 Agent Query 后，可查看个人与团队 Memory 命中及最终注入文本。</Empty>
            </div>
          )}
        </section>
      ) : (
        <section className="rounded-lg border border-border bg-surface">
          <div className="border-b border-line bg-blue-500/5 px-4 py-2 text-[11px] leading-relaxed text-blue-700">
            真回放 A/B：在同一 Source Session 上下文中，Baseline 注入已保存版本、Candidate 注入当前草稿，
            各跑一次 Agent，对比轮次 / Tool / Tokens 与 Checklist 完成度。
            {!isDirty && " 当前草稿与已保存版本一致，请先在左侧编辑记忆再回放。"}
          </div>
          <div className="space-y-3 border-b border-line px-4 py-3">
            <label className="block text-xs font-semibold text-muted-foreground">
              Agent Query
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="mt-1"
                placeholder="Agent 要完成的任务，例如：为客户 A 公司起草迁移方案"
              />
            </label>
            <label className="block text-xs font-semibold text-muted-foreground">
              完成 Checklist（每行一条）
              <Textarea
                value={replayChecklist}
                onChange={(event) => setReplayChecklist(event.target.value)}
                rows={4}
                className="mono mt-1 text-xs"
                placeholder={"例如：\n引用了正确的迁移流程\n输出包含关键联系人"}
              />
            </label>
            <div className="flex flex-wrap items-end gap-3">
              <label className="min-w-[280px] flex-1 text-xs font-semibold text-muted-foreground">
                指定 Source Session（可选）
                <Input
                  value={replaySessionId}
                  onChange={(event) => setReplaySessionId(event.target.value)}
                  className="mt-1"
                  placeholder="留空则自动选取近期可回放会话"
                />
              </label>
              <Button
                onClick={() => void runReplay()}
                disabled={replaying || !isDirty || !query.trim() || !replayChecklist.trim()}
              >
                <FlaskConical className="size-4" />
                {replaying ? "回放中…" : "运行真回放 A/B"}
              </Button>
            </div>
          </div>
          <div className="p-4">
            {replaying ? (
              <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在并行执行 Baseline / Candidate 分支…
              </div>
            ) : replayResult ? (
              <MemoryReplayResultView result={replayResult} />
            ) : (
              <Empty>填写 Query 与 Checklist 后运行，可对比改动前后 Agent 的真实执行差异。</Empty>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

const MEMORY_METRICS = [
  { key: "interaction_turns", label: "轮次" },
  { key: "tool_call_count", label: "Tool 调用" },
  { key: "total_tokens", label: "Tokens" },
] as const;

function MemoryReplayResultView({ result }: { result: MemoryReplayResult }) {
  const dimensions = result.efficiency?.dimensions || {};
  const verdictTone =
    result.verdict === "accept" ? "green" : result.verdict === "reject" ? "red" : "amber";
  const verdictLabel =
    result.verdict === "accept" ? "改动更优" : result.verdict === "reject" ? "改动退化" : "无显著差异";
  const replayCase = result.cases?.[0];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={verdictTone}>{verdictLabel}</Pill>
        <Pill tone={result.status === "evaluated" ? "green" : "red"}>
          {result.status === "evaluated" ? "已评估" : "失败"}
        </Pill>
        {result.reason && (
          <span className="text-[11px] text-muted-foreground">{result.reason}</span>
        )}
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        {MEMORY_METRICS.map((metric) => {
          const dim = dimensions[metric.key] || {};
          const delta = Number(dim.delta || 0);
          return (
            <div key={metric.key} className="rounded-lg border border-border bg-surface p-3">
              <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">
                {metric.label}
              </div>
              <div className="flex items-baseline gap-1.5 text-base font-bold">
                <span>{Number(dim.baseline || 0).toLocaleString()}</span>
                <span className="text-muted-soft">→</span>
                <span>{Number(dim.candidate || 0).toLocaleString()}</span>
              </div>
              <div
                className={cn(
                  "mt-1 text-[11px] font-semibold",
                  delta > 0 ? "text-emerald-600" : delta < 0 ? "text-rose-600" : "text-muted-foreground",
                )}
              >
                {delta > 0 ? `减少 ${delta.toLocaleString()}` : delta < 0 ? `增加 ${Math.abs(delta).toLocaleString()}` : "持平"}
              </div>
            </div>
          );
        })}
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <MemoryReplayBranchView title="Baseline · 已保存版本" branch={replayCase?.baseline} tone="gray" />
        <MemoryReplayBranchView title="Candidate · 当前草稿" branch={replayCase?.candidate} tone="blue" />
      </div>
    </div>
  );
}

function MemoryReplayBranchView({
  title,
  branch,
  tone,
}: {
  title: string;
  branch?: MemoryReplayBranch;
  tone: "gray" | "blue";
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-background/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface-subtle px-4 py-2.5">
        <span className="text-xs font-semibold">{title}</span>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone={tone}>{branch?.interaction_turns ?? 0} 轮</Pill>
          <Pill tone={tone}>{branch?.tool_call_count ?? 0} tools</Pill>
          <Pill tone={tone}>{Number(branch?.total_tokens || 0).toLocaleString()} tokens</Pill>
          <Pill tone={branch?.ok ? "green" : "red"}>{branch?.ok ? "完成" : "失败"}</Pill>
        </div>
      </div>
      {branch?.error && (
        <pre className="mono whitespace-pre-wrap break-words border-b border-line bg-rose-50 p-3 text-[11px] text-rose-700">
          {branch.error}
        </pre>
      )}
      <pre className="mono max-h-[280px] overflow-auto whitespace-pre-wrap break-words p-3 text-[11px] leading-6">
        {branch?.final_response || "（无输出）"}
      </pre>
    </section>
  );
}
