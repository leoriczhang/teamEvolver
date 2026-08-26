import { useCallback, useEffect, useRef, useState } from "react";
import { Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, type AggregationSettings, type AggregationSettingsUpdate, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { RefreshCw, Play, Users, CheckSquare, Square } from "lucide-react";

type GroupStatus = "ok" | "skipped" | "failed";

type AggregationGroup = {
  group_key: string;
  kind: string;
  target_uri: string;
  source_count: number;
  status: GroupStatus;
  detail?: string;
};

type AggregationRun = {
  task_id: string;
  account_id: string;
  endpoint: string;
  auth_mode: "trusted" | "api_key";
  target_uri: string;
  status: "pending" | "running" | "completed" | "failed";
  started_at?: number;
  finished_at?: number | null;
  error?: string;
  groups: AggregationGroup[];
};

type OkfSkill = {
  skill_name?: string;
  skill_uri?: string;
  body: string;
  editable: boolean;
};

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
};

const GROUP_LABEL: Record<GroupStatus, string> = {
  ok: "已更新",
  skipped: "未变更",
  failed: "失败",
};

export default function TeamMemoryAggregationView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [accountId, setAccountId] = useState("");
  const [mode, setMode] = useState<"incremental" | "full">("incremental");
  const [targetUri, setTargetUri] = useState("");
  const [users, setUsers] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [listing, setListing] = useState(false);
  const [run, setRun] = useState<AggregationRun | null>(null);
  const [skill, setSkill] = useState<OkfSkill | null>(null);
  const [triggering, setTriggering] = useState(false);
  const [editingSkill, setEditingSkill] = useState(false);
  const [skillDraft, setSkillDraft] = useState("");
  const [savingSkill, setSavingSkill] = useState(false);
  const pollRef = useRef<number | null>(null);
  const loaded = useRef(false);

  const [settings, setSettings] = useState<AggregationSettings | null>(null);
  const [prefixInput, setPrefixInput] = useState("");
  const [prefixDirty, setPrefixDirty] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [settingsError, setSettingsError] = useState("");

  const isAdmin = String(user?.role || "") === "admin";

  const loadSettings = useCallback(async () => {
    setSettingsLoaded(false);
    setSettingsError("");
    try {
      const data = await api<AggregationSettings>("/api/aggregation/settings");
      setSettings(data);
      setPrefixInput(data.shared_knowledge_prefix);
      setTargetUri((current) => current.trim() ? current : data.target_root);
      setPrefixDirty(false);
      setSettingsLoaded(true);
    } catch (e: any) {
      setSettings(null);
      setSettingsError(e.message || "无法加载配置");
      setSettingsLoaded(true);
    }
  }, []);

  const saveSettings = useCallback(async () => {
    if (!prefixDirty || !settings) return;
    setSavingSettings(true);
    try {
      const body: AggregationSettingsUpdate = {
        shared_knowledge_prefix: prefixInput.trim().replace(/^\/+|\/+$/g, ""),
      };
      const data = await api<AggregationSettings>("/api/aggregation/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const previousDefault = settings.target_root;
      setSettings(data);
      setPrefixInput(data.shared_knowledge_prefix);
      setTargetUri((current) =>
        !current.trim() || current === previousDefault
          ? data.target_root
          : current
      );
      setPrefixDirty(false);
      toastOk("已保存", `输出目录已更新为 ${data.target_root}`);
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSavingSettings(false);
    }
  }, [prefixDirty, settings, prefixInput]);

  const loadSkill = useCallback(async () => {
    try {
      const data = await api<OkfSkill>("/api/aggregation/okf-skill");
      setSkill(data);
    } catch {
      setSkill(null);
    }
  }, []);

  function beginEditSkill() {
    setSkillDraft(skill?.body || "");
    setEditingSkill(true);
  }

  function cancelEditSkill() {
    setEditingSkill(false);
    setSkillDraft("");
  }

  async function saveSkill() {
    setSavingSkill(true);
    try {
      const data = await api<{ ok: boolean; body: string }>(
        "/api/aggregation/okf-skill",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body: skillDraft }),
        }
      );
      setSkill((prev) => (prev ? { ...prev, body: data.body } : prev));
      setEditingSkill(false);
      toastOk("已保存", "团队记忆聚合 Skill 已更新，下次聚合生效");
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSavingSkill(false);
    }
  }

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const pollStatus = useCallback(
    (taskId: string) => {
      stopPolling();
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api<AggregationRun>(
            `/api/aggregation/status/${encodeURIComponent(taskId)}`
          );
          setRun(next);
          if (next.status === "completed" || next.status === "failed") {
            stopPolling();
            if (next.status === "completed") {
              toastOk("聚合完成", `${next.groups.length} 个分组`);
            } else {
              toastErr("聚合失败", next.error || "");
            }
          }
        } catch (e: any) {
          stopPolling();
          toastErr("状态查询失败", e.message);
        }
      }, 2000);
    },
    [stopPolling]
  );

  // On mount: load skill preview + recover any in-flight/last run so a page
  // refresh does not lose the aggregation progress.
  useEffect(() => {
    if (!active || loaded.current) return;
    loaded.current = true;
    loadSkill();
    loadSettings();
    (async () => {
      try {
        const data = await api<{ runs: AggregationRun[] }>("/api/aggregation/runs");
        const runs = data.runs || [];
        if (runs.length > 0) {
          const latest = runs[0];
          setRun(latest);
          if (latest.account_id) setAccountId((prev) => prev || latest.account_id);
          if (latest.target_uri) setTargetUri(latest.target_uri);
          if (latest.status === "running" || latest.status === "pending") {
            pollStatus(latest.task_id);
          }
        }
      } catch {
        // no active runs / not reachable — ignore
      }
    })();
  }, [active, loadSkill, loadSettings, pollStatus]);

  useEffect(() => stopPolling, [stopPolling]);

  function clearUserSelection() {
    setUsers(null);
    setSelected(new Set());
  }

  async function listUsers() {
    const account = accountId.trim();
    setListing(true);
    try {
      const data = await api<{
        endpoint: string;
        account_id: string;
        auth_mode: "trusted" | "api_key";
        users: string[];
      }>(
        "/api/aggregation/users",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            account_id: account || undefined,
          }),
        }
      );
      const list = data.users || [];
      setUsers(list);
      setSelected(new Set(list)); // default: all selected
      if (!account && data.account_id) setAccountId(data.account_id);
      toastOk("已列出用户", `${list.length} 个可聚合用户`);
    } catch (e: any) {
      setUsers(null);
      toastErr("列出用户失败", e.message);
    } finally {
      setListing(false);
    }
  }

  function toggleUser(uid: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  }

  function selectAll() {
    setSelected(new Set(users || []));
  }
  function selectNone() {
    setSelected(new Set());
  }
  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<string>();
      for (const u of users || []) if (!prev.has(u)) next.add(u);
      return next;
    });
  }

  async function confirmAggregate() {
    const account = accountId.trim();
    const userIds = Array.from(selected);
    const outputUri = targetUri.trim();
    if (userIds.length === 0) {
      toastErr("未选择用户", "请至少勾选一个用户");
      return;
    }
    if (!outputUri) {
      toastErr("未指定输出 URI", "请输入 viking://resources/ 下的目标路径");
      return;
    }
    setTriggering(true);
    try {
      const started = await api<AggregationRun>("/api/aggregation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: account || undefined,
          mode,
          target_uri: outputUri,
          user_ids: userIds,
        }),
      });
      setRun(started);
      toastOk("聚合已触发", `${userIds.length} 个用户 · ${started.task_id}`);
      pollStatus(started.task_id);
    } catch (e: any) {
      toastErr("触发聚合失败", e.message);
    } finally {
      setTriggering(false);
    }
  }

  if (!isAdmin) {
    return (
      <Panel title="团队记忆聚合">
        <div className="p-3.5">
          <p className="text-sm text-muted-foreground">
            团队记忆聚合需要管理员权限。请使用管理员账号操作。
          </p>
        </div>
      </Panel>
    );
  }

  const running = run?.status === "running" || run?.status === "pending";
  const allSelected = users != null && selected.size === users.length && users.length > 0;
  const currentTargetLabel = !settingsLoaded
    ? "加载中…"
    : targetUri.trim() || settings?.target_root || "未指定";
  const currentSkillLabel = settings?.okf_skill_uri || skill?.skill_name || "当前聚合 Skill";

  return (
    <div className="mt-3 flex flex-col gap-3">
      <Panel title="跨 User 记忆聚合">
        <div className="flex flex-col gap-3 p-3.5">
          <div className="text-[13px] leading-relaxed text-muted-foreground">
            将一个 OpenViking Account 下选定 User 的个人记忆，通过 ov compile 聚合为{" "}
            <code>{currentTargetLabel}</code>
            下的团队共享记忆；具体输出格式与页面结构由{" "}
            <code>{currentSkillLabel}</code>
            定义。流程：输入 Account → 列出用户 → 勾选/反选 → 确认聚合。
            产物在 account 内全员可检索，每次任务可独立指定输出 URI。
            控制台默认使用系统配置的 Trusted Root Key。
          </div>

          {/* Step 1: account + list users */}
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-[12px] font-[700]">
              OpenViking Account ID（可选）
              <Input
                value={accountId}
                disabled={running}
                onChange={(e) => {
                  setAccountId(e.target.value);
                  clearUserSelection();
                }}
                placeholder="留空则使用当前配置的 account"
                className="w-[280px]"
              />
            </label>
            <label className="flex flex-col gap-1 text-[12px] font-[700]">
              模式
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as "incremental" | "full")}
                className="h-9 rounded-md border border-border bg-surface px-2 text-[13px]"
              >
                <option value="incremental">增量（仅变更/失败用户）</option>
                <option value="full">全量（强制全部重编译）</option>
              </select>
            </label>
            <label className="flex min-w-[320px] flex-1 flex-col gap-1 text-[12px] font-[700]">
              本次输出 URI
              <Input
                value={targetUri}
                disabled={running}
                placeholder="viking://resources/shared-knowledge"
                onChange={(e) => setTargetUri(e.target.value)}
                className="font-mono"
              />
            </label>
            <Button onClick={listUsers} disabled={listing}>
              <Users className="mr-1.5 size-4" />
              {listing ? "列出中…" : "列出用户"}
            </Button>
            <Button variant="outline" onClick={loadSkill} title="刷新 OKF Skill 预览">
              <RefreshCw className="mr-1.5 size-4" />
              刷新 Skill
            </Button>
          </div>

          {/* Step 2: user selection */}
          {users != null && (
            <div className="rounded-md border border-border">
              <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-3 py-2">
                <span className="text-[12px] font-[700]">
                  可聚合用户 {selected.size}/{users.length}
                </span>
                <div className="ml-auto flex gap-2">
                  <Button size="sm" variant="outline" onClick={selectAll}>
                    <CheckSquare className="mr-1 size-3.5" /> 全选
                  </Button>
                  <Button size="sm" variant="outline" onClick={selectNone}>
                    <Square className="mr-1 size-3.5" /> 全不选
                  </Button>
                  <Button size="sm" variant="outline" onClick={invertSelection}>
                    反选
                  </Button>
                </div>
              </div>
              {users.length === 0 ? (
                <div className="px-3 py-3 text-[12px] text-muted-foreground">
                  该 account 下没有可聚合的用户。
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 p-3 sm:grid-cols-3 md:grid-cols-4">
                  {users.map((uid) => (
                    <label
                      key={uid}
                      className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-[12px] hover:bg-muted/50"
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(uid)}
                        onChange={() => toggleUser(uid)}
                        className="size-3.5"
                      />
                      <span className="truncate font-mono" title={uid}>
                        {uid}
                      </span>
                    </label>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-3 border-t border-border px-3 py-2">
                <Button
                  onClick={confirmAggregate}
                  disabled={triggering || running || selected.size === 0}
                >
                  <Play className="mr-1.5 size-4" />
                  {triggering
                    ? "触发中…"
                    : running
                      ? "聚合进行中…"
                      : `确认聚合（${selected.size} 用户）`}
                </Button>
                {allSelected && (
                  <span className="text-[12px] text-muted-foreground">已全选</span>
                )}
              </div>
            </div>
          )}
        </div>
      </Panel>

      <Panel
        title="默认输出目录配置"
        extra={
          <span className="text-[12px] text-muted-foreground">
            当前：<code>{currentTargetLabel}</code>
          </span>
        }
      >
        <div className="p-3.5">
          {!settingsLoaded ? (
            <div className="text-[12px] text-muted-foreground">加载中…</div>
          ) : settings ? (
            <div className="flex flex-col gap-3">
              <div className="grid gap-3 md:grid-cols-2">
                <label className="flex flex-col gap-1 text-[12px] font-[700]">
                  团队记忆前缀（输出目录）
                  <Input
                    value={prefixInput}
                    disabled={!isAdmin || savingSettings}
                    placeholder="shared-knowledge"
                    onChange={(e) => {
                      setPrefixInput(e.target.value);
                      setPrefixDirty(true);
                    }}
                  />
                  <span className="text-[11px] font-normal text-muted-foreground">
                    最终路径：<code className="mono break-all">viking://resources/{prefixInput.trim().replace(/^\/+|\/+$/g, "") || "shared-knowledge"}</code>
                  </span>
                </label>
                <div className="flex flex-col gap-1 text-[12px] font-[700]">
                  其他信息
                  <div className="rounded-md border border-border bg-surface p-2 font-normal text-[11px] text-muted-foreground">
                    <div>staging 目录：<code>{settings.work_root}</code></div>
                    <div>OKF Skill URI：<code>{settings.okf_skill_uri}</code></div>
                    <div>Key Seed：<code>{settings.key_seed}</code></div>
                  </div>
                </div>
              </div>

              {isAdmin ? (
                <div className="flex justify-end gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={savingSettings || !prefixDirty}
                    onClick={() => {
                      setPrefixInput(settings.shared_knowledge_prefix);
                      setPrefixDirty(false);
                    }}
                  >
                    重置
                  </Button>
                  <Button
                    size="sm"
                    disabled={savingSettings || !prefixDirty}
                    onClick={saveSettings}
                  >
                    {savingSettings ? "保存中…" : "保存输出目录"}
                  </Button>
                </div>
              ) : (
                <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
                  仅管理员可修改聚合输出目录。
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface p-3">
              <div className="text-[12px] text-muted-foreground">
                无法加载配置{settingsError ? `：${settingsError}` : ""}
              </div>
              <Button size="sm" variant="outline" onClick={loadSettings}>
                <RefreshCw className="mr-1.5 size-4" />
                重试
              </Button>
            </div>
          )}
        </div>
      </Panel>

      {run && (
        <Panel
          title={`任务 ${run.task_id}`}
          count={STATUS_LABEL[run.status] || run.status}
          extra={
            <span className="text-[12px] text-muted-foreground">
              account: {run.account_id}
            </span>
          }
        >
          <div className="p-3.5">
            {run.error && (
              <div className="mb-2 rounded bg-rose-50 p-2 text-[12px] text-rose-700">
                {run.error}
              </div>
            )}
            <div className="mb-2 break-all font-mono text-[11px] text-muted-foreground">
              OpenViking：{run.endpoint} · {run.auth_mode}
              <br />
              输出：{run.target_uri}
            </div>
            <div className="overflow-hidden rounded-md border border-border">
              <table className="w-full text-[12px]">
                <thead className="bg-muted/40 text-left font-[700]">
                  <tr>
                    <th className="px-3 py-2">分组</th>
                    <th className="px-3 py-2">目标目录</th>
                    <th className="px-3 py-2">来源数</th>
                    <th className="px-3 py-2">状态</th>
                    <th className="px-3 py-2">备注</th>
                  </tr>
                </thead>
                <tbody>
                  {run.groups.length === 0 && (
                    <tr>
                      <td className="px-3 py-3 text-muted-foreground" colSpan={5}>
                        {run.status === "running" ? "正在处理…" : "无分组结果"}
                      </td>
                    </tr>
                  )}
                  {run.groups.map((g) => (
                    <tr key={g.group_key} className="border-t border-border">
                      <td className="px-3 py-2 font-[700]">{g.group_key}</td>
                      <td className="px-3 py-2 font-mono text-[11px]">{g.target_uri}</td>
                      <td className="px-3 py-2">{g.source_count}</td>
                      <td className="px-3 py-2">
                        <span
                          className={cn(
                            "rounded px-2 py-0.5 text-[11px] font-[700]",
                            g.status === "ok" && "bg-emerald-100 text-emerald-700",
                            g.status === "skipped" && "bg-slate-100 text-slate-600",
                            g.status === "failed" && "bg-rose-100 text-rose-700"
                          )}
                        >
                          {GROUP_LABEL[g.status]}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{g.detail || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </Panel>
      )}

      {skill && (
        <Panel
          title="团队记忆聚合 Skill"
          extra={
            <div className="flex items-center gap-2">
              <code className="text-[11px] text-muted-foreground">
                {skill.skill_name || skill.skill_uri}
              </code>
              {!editingSkill ? (
                <Button size="sm" variant="outline" onClick={beginEditSkill}>
                  编辑
                </Button>
              ) : (
                <>
                  <Button size="sm" onClick={saveSkill} disabled={savingSkill}>
                    {savingSkill ? "保存中…" : "保存"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={cancelEditSkill}
                    disabled={savingSkill}
                  >
                    取消
                  </Button>
                </>
              )}
            </div>
          }
        >
          <div className="p-3.5">
            <p className="text-[12px] text-muted-foreground">
              该 Skill 定义聚合输出格式、页面结构、交叉引用与聚合规则，
              供 ov compile 消费。聚合运行时会自动安装到各身份的 skills 空间。
              {editingSkill ? "编辑后点击保存，下次聚合生效。" : "点击右上角「编辑」可修改。"}
            </p>
            {editingSkill ? (
              <textarea
                value={skillDraft}
                onChange={(e) => setSkillDraft(e.target.value)}
                spellCheck={false}
                className="mt-2 h-[420px] w-full resize-y rounded-md border border-border bg-surface p-3 font-mono text-[11px] leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-foreground/10"
              />
            ) : (
              <pre className="mt-2 max-h-[360px] overflow-auto rounded-md border border-border bg-muted/30 p-3 text-[11px] leading-relaxed">
                {skill.body}
              </pre>
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}
