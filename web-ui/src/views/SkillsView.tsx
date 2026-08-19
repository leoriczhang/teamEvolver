import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Panel,
  StatCard,
  Pill,
  Empty,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { fmtTime } from "@/lib/format";
import { toastOk, toastErr } from "@/lib/toast";
import {
  api,
  cloudNote,
  type PublishRequest,
  type PublishRequestListResp,
  type ShareResult,
  type SkillListItem,
  type SkillListResp,
  type UserProfile,
  type UsersListResp,
} from "@/api/client";
import SkillEditModal from "./skills/SkillEditModal";
import PersonalSkillEditModal from "./skills/PersonalSkillEditModal";
import ImportZipModal from "./skills/ImportZipModal";
import { cn } from "@/lib/utils";

type SkillSubPage = "personal" | "team" | "requests";
type ShareDirection = "personal_to_team" | "team_to_personal";

export default function SkillsView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [subPage, setSubPage] = useState<SkillSubPage>("personal");
  const [teamResp, setTeamResp] = useState<SkillListResp | null>(null);
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUserId, setActiveUserId] = useState("");
  const [personalSkills, setPersonalSkills] = useState<SkillListItem[]>([]);
  const [teamSpaceSkills, setTeamSpaceSkills] = useState<SkillListItem[]>([]);
  const [selectedPersonal, setSelectedPersonal] = useState<Set<string>>(new Set());
  const [selectedTeam, setSelectedTeam] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [sharingNow, setSharingNow] = useState(false);
  const [editName, setEditName] = useState<string | null | undefined>(undefined);
  const [personalEditName, setPersonalEditName] = useState<string | null | undefined>(undefined);
  const [importing, setImporting] = useState(false);
  const [publishRequests, setPublishRequests] = useState<PublishRequest[]>([]);
  const loaded = useRef(false);

  const activeUser = useMemo(
    () => users.find((u) => u.id === activeUserId) || null,
    [users, activeUserId]
  );
  const isAdmin = user?.role === "admin";
  const canEditTeam = !!isAdmin;
  const teamSkills = teamResp?.skills || [];
  const sharing = !!teamResp?.sharing_enabled;
  const pendingRequestCount = publishRequests.filter((r) => r.status === "pending").length;
  // A user may edit their own personal space; admins may edit anyone's.
  const canEditPersonal = !!activeUser && (isAdmin || activeUser.id === user?.id);

  const refreshUsers = useCallback(async () => {
    const data = await api<UsersListResp>("/api/users");
    const list = data.users || [];
    setUsers(list);
    const saved = window.localStorage.getItem("teamEvolver.activeUserId") || "";
    const next = (saved && list.some((u) => u.id === saved) && saved) || list[0]?.id || "";
    setActiveUserId((cur) => (cur && list.some((u) => u.id === cur) ? cur : next));
    return next;
  }, []);

  const refreshTeam = useCallback(async () => {
    const data = await api<SkillListResp>("/api/skills");
    setTeamResp(data);
  }, []);

  const refreshRequests = useCallback(async () => {
    try {
      const data = await api<PublishRequestListResp>("/api/skill-publish-requests");
      setPublishRequests(data.requests || []);
    } catch {
      setPublishRequests([]);
    }
  }, []);

  const refreshUserSpace = useCallback(async (userId: string, space: "personal" | "team") => {
    if (!userId) {
      if (space === "personal") setPersonalSkills([]);
      else setTeamSpaceSkills([]);
      return;
    }
    const data = await api<{ skills: SkillListItem[] }>(
      `/api/users/${encodeURIComponent(userId)}/skills?space=${space}`
    );
    if (space === "personal") {
      setPersonalSkills(data.skills || []);
      setSelectedPersonal(new Set());
    } else {
      setTeamSpaceSkills(data.skills || []);
      setSelectedTeam(new Set());
    }
  }, []);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      const userId = await refreshUsers();
      await Promise.all([
        refreshTeam(),
        refreshRequests(),
        refreshUserSpace(userId, "personal"),
        refreshUserSpace(userId, "team"),
      ]);
    } catch (e: any) {
      toastErr("加载技能失败", e.message);
    } finally {
      setLoading(false);
    }
  }, [refreshTeam, refreshRequests, refreshUserSpace, refreshUsers]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refreshAll();
    }
  }, [active, refreshAll]);

  useEffect(() => {
    if (!active || !activeUserId) return;
    refreshUserSpace(activeUserId, "personal").catch((e) => toastErr("加载个人技能失败", e.message));
    refreshUserSpace(activeUserId, "team").catch((e) => toastErr("加载团队空间失败", e.message));
  }, [active, activeUserId, refreshUserSpace]);

  function chooseUser(id: string) {
    setActiveUserId(id);
    window.localStorage.setItem("teamEvolver.activeUserId", id);
  }

  function requireAdmin(): boolean {
    if (canEditTeam) return true;
    toastErr("权限不足", "只有管理员可以编辑团队技能");
    return false;
  }

  async function share(direction: ShareDirection, names: string[]) {
    if (!activeUserId) {
      toastErr("请先选择用户");
      return;
    }
    if (!names.length) {
      toastErr("请选择技能");
      return;
    }
    // Non-admins cannot publish directly; they submit a request for review.
    if (direction === "personal_to_team" && !isAdmin) {
      await submitPublishRequest(names);
      return;
    }
    setSharingNow(true);
    try {
      const result = await api<ShareResult>(`/api/users/${encodeURIComponent(activeUserId)}/share`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction, skill_names: names }),
      });
      toastOk("分享完成", `上传 ${result.uploaded || 0}，跳过 ${result.skipped || 0}`);
      await refreshAll();
    } catch (e: any) {
      toastErr("分享失败", e.message);
    } finally {
      setSharingNow(false);
    }
  }

  async function submitPublishRequest(names: string[]) {
    if (!activeUserId || !names.length) {
      toastErr("请选择要申请发布的技能");
      return;
    }
    setSharingNow(true);
    try {
      await api<PublishRequest>(
        `/api/users/${encodeURIComponent(activeUserId)}/publish-requests`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_names: names }),
        },
      );
      toastOk("已提交发布申请", `等待管理员确认（${names.length} 个技能）`);
      setSelectedPersonal(new Set());
      await refreshRequests();
    } catch (e: any) {
      toastErr("提交申请失败", e.message);
    } finally {
      setSharingNow(false);
    }
  }

  async function decideRequest(requestId: string, action: "approve" | "reject") {
    const verb = action === "approve" ? "批准" : "驳回";
    if (!window.confirm(`确认${verb}该发布申请？`)) return;
    try {
      await api<PublishRequest>(
        `/api/skill-publish-requests/${encodeURIComponent(requestId)}/${action}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      toastOk(`已${verb}发布申请`);
      await refreshAll();
    } catch (e: any) {
      toastErr(`${verb}失败`, e.message);
    }
  }

  async function delPersonalSkill(name: string) {
    if (!activeUserId) return;
    if (!window.confirm(`确认删除个人技能「${name}」？`)) return;
    try {
      await api(
        `/api/users/${encodeURIComponent(activeUserId)}/skills/${encodeURIComponent(name)}?space=personal`,
        { method: "DELETE" },
      );
      toastOk("已删除个人技能", name);
      await refreshUserSpace(activeUserId, "personal");
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  async function delTeamSkill(name: string) {
    if (!requireAdmin()) return;
    if (!window.confirm(`确认删除团队技能「${name}」？此操作会同步删除团队空间资产。`)) return;
    try {
      const r = await api<{ cloud?: any }>(`/api/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
      toastOk("已删除技能", cloudNote(r.cloud));
      await refreshAll();
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  const selectedPersonalNames = Array.from(selectedPersonal);
  const selectedTeamNames = Array.from(selectedTeam);

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      <div className="mb-5 flex flex-wrap items-center justify-end gap-2">
        <select
            value={activeUserId}
            onChange={(e) => chooseUser(e.target.value)}
            className="h-8 rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
          >
            {!users.length && <option value="">未注册用户</option>}
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {(u.display_name || u.id) + (u.role === "admin" ? " · 管理员" : " · 一般用户")}
              </option>
            ))}
        </select>
        <Button variant="outline" size="sm" onClick={refreshAll} disabled={loading}>
          刷新
        </Button>
      </div>

      <div className="mb-5 flex flex-wrap gap-2">
        <SubTab active={subPage === "personal"} onClick={() => setSubPage("personal")}>
          个人技能
        </SubTab>
        <SubTab active={subPage === "team"} onClick={() => setSubPage("team")}>
          团队技能
        </SubTab>
        <SubTab active={subPage === "requests"} onClick={() => setSubPage("requests")}>
          发布审批{pendingRequestCount ? ` (${pendingRequestCount})` : ""}
        </SubTab>
      </div>

      {subPage === "personal" ? (
        <PersonalSkillsPage
          activeUser={activeUser}
          skills={personalSkills}
          selected={selectedPersonal}
          setSelected={setSelectedPersonal}
          isAdmin={!!isAdmin}
          canEdit={canEditPersonal}
          sharingNow={sharingNow}
          onRefresh={() => activeUserId && refreshUserSpace(activeUserId, "personal")}
          onPublish={() => share("personal_to_team", selectedPersonalNames)}
          onCreate={() => setPersonalEditName(null)}
          onEdit={(name) => setPersonalEditName(name)}
          onDelete={delPersonalSkill}
        />
      ) : subPage === "team" ? (
        <TeamSkillsPage
          activeUser={activeUser}
          isAdmin={!!isAdmin}
          sharing={sharing}
          skills={teamSkills}
          userTeamSkills={teamSpaceSkills}
          selectedTeam={selectedTeam}
          setSelectedTeam={setSelectedTeam}
          sharingNow={sharingNow}
          onRefresh={refreshAll}
          onCreate={() => requireAdmin() && setEditName(null)}
          onImport={() => requireAdmin() && setImporting(true)}
          onEdit={(name) => canEditTeam && setEditName(name)}
          onDelete={delTeamSkill}
          onShareToPersonal={() => share("team_to_personal", selectedTeamNames)}
        />
      ) : (
        <RequestsPage
          requests={publishRequests}
          isAdmin={!!isAdmin}
          onRefresh={refreshRequests}
          onDecide={decideRequest}
        />
      )}

      <SkillEditModal name={editName} open={editName !== undefined} onClose={() => setEditName(undefined)} onSaved={refreshAll} />
      <PersonalSkillEditModal
        userId={activeUserId}
        name={personalEditName}
        open={personalEditName !== undefined}
        onClose={() => setPersonalEditName(undefined)}
        onSaved={() => activeUserId && refreshUserSpace(activeUserId, "personal")}
      />
      <ImportZipModal open={importing} onClose={() => setImporting(false)} onImported={refreshAll} />
    </div>
  );
}

function PersonalSkillsPage({
  activeUser,
  skills,
  selected,
  setSelected,
  isAdmin,
  canEdit,
  sharingNow,
  onRefresh,
  onPublish,
  onCreate,
  onEdit,
  onDelete,
}: {
  activeUser: UserProfile | null;
  skills: SkillListItem[];
  selected: Set<string>;
  setSelected: (next: Set<string>) => void;
  isAdmin: boolean;
  canEdit: boolean;
  sharingNow: boolean;
  onRefresh: () => void;
  onPublish: () => void;
  onCreate: () => void;
  onEdit: (name: string) => void;
  onDelete: (name: string) => void;
}) {
  const categories = new Set(skills.map((s) => s.category || "general"));
  // Admins publish directly; everyone else submits a request for approval.
  const publishLabel = isAdmin ? "发布到团队" : "申请发布";
  return (
    <>
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="个人技能" value={skills.length} />
        <StatCard label="分类数" value={categories.size} />
        <StatCard label="当前用户" value={activeUser ? activeUser.display_name || activeUser.id : "未选择"} />
        <StatCard label="发布方式" value={isAdmin ? "可直接发布" : "申请后管理员确认"} />
      </div>

      <Panel
        title="个人技能列表"
        count={`(${skills.length})`}
        extra={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh}>刷新个人技能</Button>
            <Button variant="outline" size="sm" disabled={!canEdit} onClick={onCreate}>+ 新建个人技能</Button>
            <Button size="sm" disabled={!activeUser || !selected.size || sharingNow} onClick={onPublish}>
              {publishLabel} ({selected.size})
            </Button>
          </div>
        }
      >
        {!activeUser ? (
          <Empty>请先选择用户。</Empty>
        ) : (
          <div className="border-b border-line bg-surface-subtle px-4 py-3 text-xs leading-relaxed text-muted-foreground">
            {canEdit
              ? isAdmin
                ? "你可以新建、编辑、删除个人技能，并直接发布到团队空间。"
                : "你可以新建、编辑、删除自己的个人技能；发布到团队需提交申请，由管理员在“发布审批”中确认。"
              : "仅可浏览该用户的个人技能。"}
          </div>
        )}
        {!activeUser ? null : !skills.length ? (
          <Empty>个人空间暂无技能。可新建个人技能，或到“团队技能”子页从团队分发到个人。</Empty>
        ) : (
          <SkillTable
            skills={skills}
            selectable
            selected={selected}
            onToggle={(name) => toggleSet(selected, setSelected, name)}
            onToggleAll={() => toggleAll(skills, selected, setSelected)}
            actions={canEdit}
            canEdit={canEdit}
            onEdit={onEdit}
            onDelete={onDelete}
          />
        )}
      </Panel>
    </>
  );
}

function TeamSkillsPage({
  activeUser,
  isAdmin,
  sharing,
  skills,
  userTeamSkills,
  selectedTeam,
  setSelectedTeam,
  sharingNow,
  onRefresh,
  onCreate,
  onImport,
  onEdit,
  onDelete,
  onShareToPersonal,
}: {
  activeUser: UserProfile | null;
  isAdmin: boolean;
  sharing: boolean;
  skills: SkillListItem[];
  userTeamSkills: SkillListItem[];
  selectedTeam: Set<string>;
  setSelectedTeam: (next: Set<string>) => void;
  sharingNow: boolean;
  onRefresh: () => void;
  onCreate: () => void;
  onImport: () => void;
  onEdit: (name: string) => void;
  onDelete: (name: string) => void;
  onShareToPersonal: () => void;
}) {
  const categories = new Set(skills.map((s) => s.category || "general"));
  const bundles = skills.filter((s) => (s.file_count || 0) > 1).length;
  const source = userTeamSkills.length ? userTeamSkills : skills;
  return (
    <>
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="团队技能" value={skills.length} />
        <StatCard label="分类数" value={categories.size} />
        <StatCard label="带附件技能" value={bundles} />
        <StatCard label="同步状态" value={sharing ? "云端同步开启" : "云端同步关闭"} />
      </div>

      <Panel
        title="团队技能操作"
        count={activeUser ? `当前用户：${activeUser.display_name || activeUser.id}` : "未选择用户"}
        extra={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh}>刷新团队技能</Button>
            <Button variant="outline" size="sm" disabled={!isAdmin} onClick={onImport}>上传 .zip</Button>
            <Button size="sm" disabled={!isAdmin} onClick={onCreate}>+ 新建团队技能</Button>
          </div>
        }
      >
        {!activeUser ? (
          <Empty>请先选择用户。</Empty>
        ) : (
          <div className="border-b border-line bg-surface-subtle px-4 py-3 text-xs leading-relaxed text-muted-foreground">
            管理员可以新建、编辑和删除团队技能；一般用户只能浏览团队技能，并可把团队技能分发到自己的个人空间。
          </div>
        )}
        <SkillTable
          skills={skills}
          actions={isAdmin}
          canEdit={isAdmin}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      </Panel>

      <Panel
        title="团队技能分发到个人"
        count={`源：${userTeamSkills.length ? "用户团队空间" : "团队技能库"}`}
        extra={
          <Button size="sm" disabled={!activeUser || !selectedTeam.size || sharingNow} onClick={onShareToPersonal}>
            分发到个人 ({selectedTeam.size})
          </Button>
        }
      >
        {!activeUser ? (
          <Empty>请先选择用户。</Empty>
        ) : !source.length ? (
          <Empty>暂无可分发的团队技能。</Empty>
        ) : (
          <SkillTable
            skills={source}
            selectable
            selected={selectedTeam}
            onToggle={(name) => toggleSet(selectedTeam, setSelectedTeam, name)}
            onToggleAll={() => toggleAll(source, selectedTeam, setSelectedTeam)}
          />
        )}
      </Panel>
    </>
  );
}

function RequestsPage({
  requests,
  isAdmin,
  onRefresh,
  onDecide,
}: {
  requests: PublishRequest[];
  isAdmin: boolean;
  onRefresh: () => void;
  onDecide: (requestId: string, action: "approve" | "reject") => void;
}) {
  const pager = usePagedItems(requests);
  const statusTone: Record<PublishRequest["status"], "amber" | "green" | "gray"> = {
    pending: "amber",
    approved: "green",
    rejected: "gray",
  };
  const statusLabel: Record<PublishRequest["status"], string> = {
    pending: "待审批",
    approved: "已批准",
    rejected: "已驳回",
  };
  return (
    <Panel
      title={isAdmin ? "个人技能发布审批" : "我的发布申请"}
      count={`(${requests.length})`}
      extra={
        <Button variant="outline" size="sm" onClick={onRefresh}>
          刷新申请
        </Button>
      }
    >
      <div className="border-b border-line bg-surface-subtle px-4 py-3 text-xs leading-relaxed text-muted-foreground">
        {isAdmin
          ? "普通用户提交的个人技能发布申请集中在此审批；批准后技能才会写入团队空间。"
          : "你提交的发布申请状态显示在这里，批准后个人技能才会进入团队空间。"}
      </div>
      {!requests.length ? (
        <Empty>暂无发布申请。</Empty>
      ) : (
        <>
          <ListViewport>
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {["申请人", "技能", "状态", "提交时间", "处理"].map((h) => (
                    <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pager.items.map((r) => (
                  <tr key={r.request_id}>
                    <td className="border-b border-line px-4 py-2.5 align-top text-[12.5px]">
                      {r.requester_name || r.requester_id}
                    </td>
                    <td className="max-w-[360px] border-b border-line px-4 py-2.5 align-top">
                      <div className="flex flex-wrap gap-1">
                        {r.skill_names.map((n) => (
                          <Pill key={n} tone="blue">{n}</Pill>
                        ))}
                      </div>
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      <Pill tone={statusTone[r.status]}>{statusLabel[r.status]}</Pill>
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top text-xs text-muted-foreground">
                      {fmtTime(r.created_at)}
                    </td>
                    <td className="border-b border-line px-4 py-2.5 align-top">
                      {isAdmin && r.status === "pending" ? (
                        <div className="flex gap-1.5">
                          <Button size="sm" onClick={() => onDecide(r.request_id, "approve")}>
                            批准
                          </Button>
                          <Button variant="destructive" size="sm" onClick={() => onDecide(r.request_id, "reject")}>
                            驳回
                          </Button>
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          {r.decided_by ? `${r.decided_by}` : "—"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ListViewport>
          <PaginationControls {...pager} onPageChange={pager.setPage} />
        </>
      )}
    </Panel>
  );
}

function SkillTable({
  skills,
  selectable,
  selected,
  onToggle,
  onToggleAll,
  actions,
  canEdit,
  onEdit,
  onDelete,
}: {
  skills: SkillListItem[];
  selectable?: boolean;
  selected?: Set<string>;
  onToggle?: (name: string) => void;
  onToggleAll?: () => void;
  actions?: boolean;
  canEdit?: boolean;
  onEdit?: (name: string) => void;
  onDelete?: (name: string) => void;
}) {
  const pager = usePagedItems(skills);
  if (!skills.length) return <Empty>暂无技能。</Empty>;
  const allChecked = !!selected && selected.size === skills.length && skills.length > 0;
  return (
    <>
      <ListViewport>
        <table className="w-full border-collapse">
          <thead>
            <tr>
              {selectable && (
                <th className="w-[44px] border-b border-line px-4 py-2.5 text-left">
                  <input type="checkbox" checked={allChecked} onChange={onToggleAll} />
                </th>
              )}
              {["名称", "分类", "描述", "附件", "更新时间"].map((h) => (
                <th key={h} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">{h}</th>
              ))}
              {actions && <th className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground" />}
            </tr>
          </thead>
          <tbody>
            {pager.items.map((s) => (
              <tr
                key={s.name}
                className={cn((canEdit || selectable) && "clickable")}
                onClick={() => (selectable ? onToggle?.(s.name) : canEdit ? onEdit?.(s.name) : undefined)}
              >
                {selectable && (
                  <td className="border-b border-line px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={!!selected?.has(s.name)} onChange={() => onToggle?.(s.name)} />
                  </td>
                )}
                <td className="mono border-b border-line px-4 py-2.5 align-top">{s.name}</td>
                <td className="border-b border-line px-4 py-2.5 align-top">
                  <Pill tone={s.category === "general" ? "gray" : "purple"}>{s.category || "general"}</Pill>
                </td>
                <td className="max-w-[480px] border-b border-line px-4 py-2.5 align-top text-[12.5px] text-muted-foreground">{s.description || ""}</td>
                <td className="border-b border-line px-4 py-2.5 align-top">
                  {(s.file_count || 0) > 1 ? <Pill tone="blue">{s.file_count} 文件</Pill> : <span className="text-xs text-muted-foreground">—</span>}
                </td>
                <td className="border-b border-line px-4 py-2.5 align-top text-xs text-muted-foreground">{fmtTime(s.updated_at)}</td>
                {actions && (
                  <td className="border-b border-line px-4 py-2.5 align-top" onClick={(e) => e.stopPropagation()}>
                    <div className="flex gap-1.5">
                      <Button variant="outline" size="sm" disabled={!canEdit} onClick={() => onEdit?.(s.name)}>编辑</Button>
                      <Button variant="destructive" size="sm" disabled={!canEdit} onClick={() => onDelete?.(s.name)}>删除</Button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </ListViewport>
      <PaginationControls {...pager} onPageChange={pager.setPage} />
    </>
  );
}

function SubTab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-lg border px-4 py-2 text-sm font-semibold transition-colors",
        active ? "border-sidebar-primary bg-sidebar-primary text-white" : "border-border bg-surface hover:bg-muted"
      )}
    >
      {children}
    </button>
  );
}

function toggleSet(current: Set<string>, setSelected: (next: Set<string>) => void, name: string) {
  const next = new Set(current);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  setSelected(next);
}

function toggleAll(skills: SkillListItem[], current: Set<string>, setSelected: (next: Set<string>) => void) {
  if (current.size === skills.length) setSelected(new Set());
  else setSelected(new Set(skills.map((s) => s.name)));
}
