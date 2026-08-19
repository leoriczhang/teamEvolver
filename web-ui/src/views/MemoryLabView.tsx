import { useCallback, useEffect, useState } from "react";
import { FileText, RefreshCw, RotateCcw } from "lucide-react";
import { api, type UserProfile, type UsersListResp } from "@/api/client";
import { Empty, Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toastErr } from "@/lib/toast";
import { cn } from "@/lib/utils";
import {
  MemoryInjectionCompare,
  type ScopeName,
  type TreeResponse,
  type WorkspaceConfig,
  type WorkspaceEntry,
} from "@/views/OpenVikingWorkspaceShell";

const MEMORY_SCOPES: { scope: ScopeName; label: string }[] = [
  { scope: "personal_memory", label: "个人 Memory" },
  { scope: "team_memory", label: "团队 Memory" },
];

// Standalone Memory Lab: pick a memory file, edit a draft, and run the same
// injection comparison / true-replay A/B available from the workspace tree.
export default function MemoryLabView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [userId, setUserId] = useState(user?.id || "");
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const [scopeName, setScopeName] = useState<ScopeName>("personal_memory");
  const [files, setFiles] = useState<WorkspaceEntry[]>([]);
  const [selectedUri, setSelectedUri] = useState("");
  const [original, setOriginal] = useState("");
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);

  const scope = config?.scopes?.[scopeName];
  const selectedFile = files.find((item) => item.uri === selectedUri);

  const loadFiles = useCallback(
    async (uid: string, chosen: ScopeName, cfg: WorkspaceConfig) => {
      const root = cfg.scopes?.[chosen]?.root_uri;
      if (!uid || !root) {
        setFiles([]);
        return;
      }
      try {
        const tree = await api<TreeResponse>(
          `/api/openviking/workspace/tree?scope=${encodeURIComponent(chosen)}&user_id=${encodeURIComponent(uid)}&uri=${encodeURIComponent(root)}`,
        );
        // Only editable memory files participate in a memory experiment.
        setFiles(
          (tree.entries || []).filter(
            (entry) => !entry.is_dir && /\.(md|markdown|txt)$/i.test(entry.name),
          ),
        );
      } catch (error: any) {
        toastErr("加载 Memory 文件失败", error.message);
        setFiles([]);
      }
    },
    [],
  );

  const loadConfig = useCallback(
    async (uid: string, chosen: ScopeName) => {
      if (!uid) return;
      setLoading(true);
      try {
        const cfg = await api<WorkspaceConfig>(
          `/api/openviking/workspace/config?user_id=${encodeURIComponent(uid)}`,
        );
        setConfig(cfg);
        await loadFiles(uid, chosen, cfg);
      } catch (error: any) {
        toastErr("读取 OpenViking 配置失败", error.message);
      } finally {
        setLoading(false);
      }
    },
    [loadFiles],
  );

  useEffect(() => {
    if (!active) return;
    api<UsersListResp>("/api/users")
      .then((result) => {
        const list = result.users || [];
        setUsers(list);
        const preferred =
          userId && list.some((item) => item.id === userId)
            ? userId
            : user?.id || list[0]?.id || "";
        setUserId(preferred);
        if (preferred) void loadConfig(preferred, scopeName);
      })
      .catch((error) => toastErr("加载用户失败", error.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  async function openFile(uri: string) {
    setSelectedUri(uri);
    setOriginal("");
    setDraft("");
    if (!uri) return;
    setLoading(true);
    try {
      const result = await api<{ content: string }>(
        `/api/openviking/workspace/content?scope=${encodeURIComponent(scopeName)}&user_id=${encodeURIComponent(userId)}&uri=${encodeURIComponent(uri)}`,
      );
      setOriginal(result.content || "");
      setDraft(result.content || "");
    } catch (error: any) {
      toastErr("读取文件失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  if (config && !config.enabled) {
    return (
      <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
        <Panel title="Memory Lab">
          <div className="p-4">
            <Empty>请先在“运行状态”中启用 OpenViking，并配置本地部署或云端 endpoint。</Empty>
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1440px] px-[22px] py-[22px]">
      <Panel
        title="实验对象"
        count="选择记忆文件后编辑草稿，可做注入对比或真回放 A/B"
        extra={
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => void loadConfig(userId, scopeName)}
          >
            <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 刷新
          </Button>
        }
      >
        <div className="flex flex-wrap items-end gap-3 p-4">
          {users.length > 1 && (
            <label className="text-xs font-semibold text-muted-foreground">
              用户
              <select
                value={userId}
                onChange={(event) => {
                  setUserId(event.target.value);
                  setSelectedUri("");
                  void loadConfig(event.target.value, scopeName);
                }}
                className="mt-1 block h-8 min-w-48 rounded-lg border border-border bg-background px-2 text-xs font-semibold"
              >
                {users.map((item) => (
                  <option key={item.id} value={item.id}>{item.display_name || item.id}</option>
                ))}
              </select>
            </label>
          )}
          <label className="text-xs font-semibold text-muted-foreground">
            记忆空间
            <select
              value={scopeName}
              onChange={(event) => {
                const next = event.target.value as ScopeName;
                setScopeName(next);
                setSelectedUri("");
                if (config) void loadFiles(userId, next, config);
              }}
              className="mt-1 block h-8 min-w-40 rounded-lg border border-border bg-background px-2 text-xs font-semibold"
            >
              {MEMORY_SCOPES.map((item) => (
                <option key={item.scope} value={item.scope}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="min-w-[280px] flex-1 text-xs font-semibold text-muted-foreground">
            记忆文件
            <select
              value={selectedUri}
              onChange={(event) => void openFile(event.target.value)}
              className="mt-1 block h-8 w-full rounded-lg border border-border bg-background px-2 text-xs"
            >
              <option value="">{files.length ? "请选择记忆文件" : "该空间暂无可编辑记忆文件"}</option>
              {files.map((item) => (
                <option key={item.uri} value={item.uri}>{item.name}</option>
              ))}
            </select>
          </label>
        </div>
      </Panel>

      {!selectedUri ? (
        <Panel title="Memory 实验">
          <div className="p-4">
            <Empty>
              <span className="flex items-center gap-2">
                <FileText className="size-4" /> 选择一个记忆文件后，可编辑草稿并对比改动前后效果。
              </span>
            </Empty>
          </div>
        </Panel>
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(520px,1.05fr)]">
          <Panel
            title="记忆草稿"
            count={draft !== original ? "与已保存版本有差异 · 作为 Candidate" : "与已保存版本一致"}
            extra={
              <Button
                variant="outline"
                size="sm"
                disabled={draft === original}
                onClick={() => setDraft(original)}
              >
                <RotateCcw className="size-3.5" /> 重置
              </Button>
            }
          >
            <div className="p-4">
              <Textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                spellCheck={false}
                className="mono h-[560px] min-h-0 max-h-[560px] resize-none overflow-y-auto [field-sizing:fixed] text-[12px] leading-relaxed"
              />
              <div className="mt-2 flex justify-between text-[11px] text-muted-foreground">
                <span>编辑内容仅用于本次实验，不会写回 OpenViking。</span>
                <span>{draft.length.toLocaleString()} 字符</span>
              </div>
            </div>
          </Panel>
          <div>
            <MemoryInjectionCompare
              userId={userId}
              scopeName={scopeName}
              scopeLabel={scope?.name === "team_memory" ? "团队 Memory" : "个人 Memory"}
              fileName={selectedFile?.name || ""}
              memoryUri={selectedUri}
              originalContent={original}
              draftContent={draft}
              isDirty={draft !== original}
            />
          </div>
        </div>
      )}
    </div>
  );
}
