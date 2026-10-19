import { api } from "@/api/client";
import { type UserProfile,type UsersListResp } from "@/features/admin/api/client";
import { toastErr,toastOk } from "@/lib/toast";
import { useCallback,useEffect,useMemo,useRef,useState } from "react";
import { GROUP_URI_PREFIX,ScopeConfig,SpaceConfig,SpaceKey,TreeResponse,WORKSPACE_SPACES,WorkspaceConfig,WorkspaceDraft,WorkspaceEntry,buildSpaceTree,filterTree,groupUri,initialSpace,parentUri } from "./OpenVikingWorkspaceShellShared";

export function useOpenVikingWorkspaceShell({
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
return { users, activeUserId, setActiveUserId, config, spaces, activeSpaceKey, entries, currentUri, setCurrentUri, selected, content, originalContent, viewMode, setViewMode, directoryLevel, setDirectoryLevel, directoryLevels, directoryLevelLoading, filter, setFilter, expanded, loading, saving, editing, reviewOpen, setReviewOpen, transfer, setTransfer, activeSpace, spaceScopes, selectedScope, draftList, dirtyUris, isDirty, canEditSelected, hasEditableScopes, activeWritableScope, terminalScope, filteredTree, loadConfig, chooseSpace, refreshTree, openEntry, updateDraftContent, beginEditing, discardEditing, finishEditing, saveDrafts };
}
