import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Toaster } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { Activity, Building2, ClipboardCheck, Filter, FolderTree, History, LayoutDashboard, BookMarked, BookOpenText, Users, SlidersHorizontal, LogOut, RefreshCw, Sparkles, Clock, Repeat2, ShieldCheck, TrendingUp, Zap, Database, ChevronsUpDown, DownloadCloud, TerminalSquare, HardDrive, ListChecks } from "lucide-react";
import {
  api,
  getActiveTenantId,
  setActiveTenantId,
  setTenantHeaderAllowed,
  type AuthStatus,
  type SharingConfig,
  type UserProfile,
  type TenantsResp,
} from "@/api/client";
import { PageHeader, Term } from "@/components/common";
import { toastErr, toastOk } from "@/lib/toast";
import { hasOpenVikingConfiguration } from "@/lib/sharing";
import DashboardView from "@/views/DashboardView";
import UsersView from "@/views/UsersView";
import ModelSettingsView from "@/views/ModelSettingsView";
import CandidateReviewView from "@/views/CandidateReviewView";
import HealthView from "@/views/HealthView";
import AuditView from "@/views/AuditView";
import ExperienceLibraryView from "@/views/ExperienceLibraryView";
import SessionFilterView from "@/views/SessionFilterView";
import DataSourcesView from "@/views/DataSourcesView";
import ObservabilityView from "@/views/ObservabilityView";
import EvolutionWorkspaceView from "@/views/EvolutionWorkspaceView";
import OpenVikingWorkspaceView from "@/views/OpenVikingWorkspaceShell";
import PlatformAssetsView from "@/views/PlatformAssetsView";
import SkillWorkbenchView from "@/views/SkillWorkbenchView";
import MiningView, { type MinePage } from "@miner/MiningView";
import KnowledgeBaseMiningView from "@miner/KnowledgeBaseMiningView";
import TenantsView from "@/views/TenantsView";
import DatasetsView from "@/views/DatasetsView";
import KeeMethodsView from "@/views/KeeMethodsView";
import MembersView from "@/views/MembersView";
import IdentityAuditView from "@/views/IdentityAuditView";

type ViewKey =
  | "ontology"
  | "mine-overview"
  | "mine-sources"
  | "mine-jobs"
  | "mine-knowledge-base"
  | "review-center"
  | "dashboard"
  | "experiences"
  | "datasource"
  | "datasets"
  | "observability"
  | "prompt-studio"
  | "health"
  | "workspace"
  | "agent-assets"
  | "platform"
  | "tenants"
  | "users"
  | "members"
  | "identity-audit"
  | "model";
type DashTab = "overview" | "candidates" | "audit" | "filter";
type MiningViewKey = "mine-overview" | "mine-sources" | "mine-jobs";
type StandaloneViewKey = Exclude<ViewKey, MiningViewKey | "dashboard">;

const MINE_PAGES: { key: MiningViewKey; page: MinePage }[] = [
  { key: "mine-overview", page: "overview" },
  { key: "mine-sources", page: "sources" },
  { key: "mine-jobs", page: "jobs" },
];



const NAV_SECTIONS: {
  id: string;
  label: string;
  items: {
    key: ViewKey;
    label: string;
    icon: typeof LayoutDashboard;
    adminOnly?: boolean;
    platformOnly?: boolean;
    requiresOpenViking?: boolean;
  }[];
}[] = [
  {
    id: "mining",
    label: "技能挖掘",
    items: [
      { key: "mine-overview", label: "挖掘总览", icon: LayoutDashboard },
      { key: "mine-sources", label: "知识源", icon: Database },
      { key: "mine-jobs", label: "Skill 挖掘", icon: ListChecks },
      { key: "mine-knowledge-base", label: "知识库挖掘", icon: BookOpenText, adminOnly: true, requiresOpenViking: true },
    ],
  },
  { id: "workspace", label: "工作台", items: [
    { key: "dashboard", label: "运行总览", icon: LayoutDashboard },
    { key: "workspace", label: "实验工作台", icon: FolderTree },
    { key: "health", label: "运行状态", icon: Activity, adminOnly: true },
  ] },
  { id: "agents", label: "智能体与能力", items: [
    { key: "agent-assets", label: "个人与团队资产", icon: BookMarked },
    { key: "platform", label: "平台资产", icon: HardDrive, adminOnly: true },
    { key: "members", label: "成员与接入", icon: Users, adminOnly: true },
  ] },
  { id: "knowledge", label: "知识与方法", items: [
    { key: "ontology", label: "Ontology 与方法 Wiki", icon: Database },
    { key: "experiences", label: "经验库", icon: BookMarked },
  ] },
  { id: "evolution", label: "进化与实验", items: [
    { key: "datasets", label: "数据集", icon: FolderTree },
    { key: "prompt-studio", label: "进化链路", icon: TerminalSquare, adminOnly: true },
  ] },
  { id: "review", label: "审查与发布", items: [
    { key: "review-center", label: "审查与发布入口", icon: ListChecks },
    { key: "identity-audit", label: "权限审计", icon: History, platformOnly: true },
    { key: "observability", label: "链路观测", icon: Activity, platformOnly: true },
  ] },
  { id: "settings", label: "设置与接入", items: [
    { key: "users", label: "平台用户", icon: Users, platformOnly: true },
    { key: "tenants", label: "租户管理", icon: Building2, platformOnly: true },
    { key: "model", label: "模型配置", icon: SlidersHorizontal, adminOnly: true },
    { key: "datasource", label: "数据源接入", icon: DownloadCloud, adminOnly: true },
  ] },
];

const VIEW_KEYS = new Set<ViewKey>(
  NAV_SECTIONS.flatMap(({ items }) => items.map(({ key }) => key))
);

function initialView(): ViewKey {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("view") as ViewKey | null;
  return requested && VIEW_KEYS.has(requested) ? requested : "dashboard";
}

// Operational views stay together under the evolution overview.
const DASH_TABS: { key: DashTab; label: string; icon: typeof LayoutDashboard }[] = [
  { key: "overview", label: "总览", icon: LayoutDashboard },
  { key: "candidates", label: "候选评审", icon: ClipboardCheck },
  { key: "audit", label: "进化审计", icon: History },
  { key: "filter", label: "过滤审计", icon: Filter },
];

type StandalonePageConfig = {
  key: StandaloneViewKey;
  title: string;
  description: ReactNode;
  badge: string;
  scope?: "global" | "tenant";
  requiresOpenViking?: boolean;
  render: (props: {
    active: boolean;
    user?: UserProfile | null;
    openVikingConfigured: boolean;
    openVikingConfigChecked: boolean;
    onSharingConfigChange: (config: SharingConfig) => void;
    onNavigate: (view: ViewKey) => void;
    activeTenantId?: string;
  }) => ReactNode;
};

const STANDALONE_PAGES: StandalonePageConfig[] = [
  {key: "review-center", title: "审查与发布", description: "按资产类型进入现有审查入口，保留各自的发布规则。", scope: "tenant", badge: "治理",
   render: ({user}) => <div className="space-y-4 rounded border p-5"><p>已发布 Skill、团队 Memory 与方法 Wiki 各自保留维护入口。</p>
     {user?.role === "admin" ? <><a className="block underline" href="?view=dashboard&tab=candidates">Skill Candidate 与 True Replay</a>
     <a className="block underline" href="?view=ontology">Ontology / 方法 Wiki 审查</a></> : <p>当前为普通读者，可读取授权版本。</p>}
     <a className="block underline" href="?view=agent-assets">团队 Skill / Memory</a></div>},
  {
    key: "members", title: "成员与接入", description: "管理当前租户的成员角色和 Agent 接入凭证。",
    badge: "租户治理", scope: "tenant",
    render: ({ active, activeTenantId }) => <MembersView active={active} tenantId={activeTenantId || ""} />,
  },
  {
    key: "identity-audit", title: "权限审计", description: "查看平台访问、成员和身份变更记录。",
    badge: "平台治理", scope: "global",
    render: ({ active }) => <IdentityAuditView active={active} />,
  },
  {
    key: "mine-knowledge-base",
    title: "知识库挖掘",
    description: null,
    badge: "OpenViking Compile",
    scope: "tenant",
    requiresOpenViking: true,
    render: ({ active, openVikingConfigured, openVikingConfigChecked, onNavigate }) =>
      openVikingConfigChecked && !openVikingConfigured ? (
        <OpenVikingRequiredNotice onConfigure={() => onNavigate("health")} />
      ) : openVikingConfigured ? (
        <KnowledgeBaseMiningView active={active} />
      ) : null,
  },
  {
    key: "ontology", title: "Ontology 与方法 Wiki", description: "编制、审核、导出方法结构与有证据的 Wiki。",
    badge: "KEE R6", scope: "tenant", render: ({ active, activeTenantId, user }) => <KeeMethodsView key={`${activeTenantId}:${user?.id}`} active={active} userId={user?.id || ""} admin={user?.role === "admin"} />,
  },
  {
    key: "experiences",
    title: "经验库",
    description: "查看 Session 已生成的 Skill 错误经验和优秀实践，不依赖 Skill 是否已上传。",
    badge: "进化闭环",
    scope: "tenant",
    render: ({ active, activeTenantId, user }) => <ExperienceLibraryView key={`${activeTenantId}:${user?.id}`}
      active={active} canManageSync={user?.role === "admin"} />,
  },
  {
    key: "datasets",
    title: "数据集",
    description: "从 Session 保存数据集，管理、导出并批量重回放验证。",
    badge: "重回放验证",
    scope: "tenant",
    render: ({ active }) => <DatasetsView active={active} />,
  },
  {
    key: "datasource",
    title: "数据源接入",
    description: "",
    badge: "上游接入",
    scope: "tenant",
    render: ({ active, user }) => <DataSourcesView active={active} user={user} />,
  },
  {
    key: "observability",
    title: "链路观测",
    description: "",
    badge: "全局",
    scope: "global",
    render: ({ active, user }) => <ObservabilityView active={active} user={user} />,
  },
  {
    key: "prompt-studio",
    title: "进化链路",
    description: (
      <>
        统一管理 Skills 自进化与团队 Memory 自进化（<Term term="memory-maintenance" />
        ）的 Prompt、模型、参数和运行状态。
      </>
    ),
    badge: "白盒配置",
    scope: "tenant",
    render: ({ active, user, openVikingConfigured }) => (
      <EvolutionWorkspaceView
        active={active}
        user={user}
        openVikingConfigured={openVikingConfigured}
      />
    ),
  },
  {
    key: "workspace",
    title: "实验工作台",
    description: "在代码文件夹中编辑 Skill，通过 True Replay 在线调试并查看执行结果。",
    badge: "Workspace",
    scope: "tenant",
    render: ({ active, user }) => <SkillWorkbenchView active={active} user={user} />,
  },
  {
    key: "agent-assets",
    title: "个人与团队资产",
    description: "浏览和维护 OpenViking 中的个人记忆与团队资源。",
    badge: "OpenViking",
    scope: "tenant",
    render: ({ active, user }) => <OpenVikingWorkspaceView active={active} user={user} />,
  },
  {
    key: "platform",
    title: "平台资产",
    description: "自进化平台自己的存储：会话队列、候选技能、验证任务、记忆变更与 Skill Lab 等中间产物，Agent 无法引用，仅供平台运行与排查。",
    badge: "平台内部",
    scope: "tenant",
    render: ({ active }) => <PlatformAssetsView active={active} />,
  },
  {
    key: "model",
    title: "模型配置",
    description: "配置当前租户的默认模型、Base URL 和 API Key；阶段可在进化链路中使用独立模型连接。",
    badge: "租户配置",
    scope: "tenant",
    render: ({ active, user }) => <ModelSettingsView active={active} user={user} />,
  },
  {
    key: "users",
    title: "用户与权限",
    description: "管理登录账号与平台身份；租户角色在「成员与接入」中分配。",
    badge: "平台治理",
    scope: "global",
    render: ({ active, user }) => <UsersView active={active} user={user} />,
  },
  {
    key: "tenants",
    title: "租户管理",
    description: "多租户模式下创建/禁用租户、轮换 agent 接入 token；单租户部署保持 default 一个租户。",
    badge: "平台治理",
    scope: "global",
    render: ({ active, user }) => <TenantsView active={active} user={user} />,
  },
  {
    key: "health",
    title: "运行状态",
    description: "汇总服务、存储、模型、Agent、用户和技能状态，定位平台运行问题。",
    badge: "平台治理",
    scope: "tenant",
    render: ({ active, user, onSharingConfigChange, activeTenantId }) => (
      <HealthView
        active={active}
        user={user}
        onSharingConfigChange={onSharingConfigChange}
        activeTenantId={activeTenantId}
      />
    ),
  },
];

const DASH_PAGE_META: Record<DashTab, { title: string; description: string }> = {
  overview: {
    title: "运行总览",
    description: "监控会话进入、技能进化、候选评审和版本发布的完整闭环。",
  },
  candidates: {
    title: "候选评审",
    description: "集中处理待发布技能候选，核对 True Replay 证据后再发布。",
  },
  audit: {
    title: "进化审计",
    description: "追踪每次进化周期消费的会话、生成的候选和上传的技能变更。",
  },
  filter: {
    title: "过滤审计",
    description: "查看会话入队前的 valuable / chitchat 判别结果，用于校准进化入口。",
  },
};

export default function App() {
  const [mineInputDir, setMineInputDir] = useState("");
  const [view, setView] = useState<ViewKey>(initialView);
  const [perspective, setPerspective] = useState("knowledge-manager");
  const [dashTab, setDashTab] = useState<DashTab>(() => {
    const tab = new URLSearchParams(window.location.search).get("tab");
    return DASH_TABS.some(item => item.key === tab) ? tab as DashTab : "overview";
  });
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [authCheckFailed, setAuthCheckFailed] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [refreshingLogin, setRefreshingLogin] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [openVikingConfigured, setOpenVikingConfigured] = useState(false);
  // False until /api/sharing-config answers: gates the "未配置" badge and the
  // placeholder page so neither flashes before the config is actually known.
  const [openVikingConfigChecked, setOpenVikingConfigChecked] = useState(false);
  // ---- Tenant context (multi-tenancy plan §4.1) ----
  // Tenant switches reload the app (see switchTenant), so plain state read at
  // mount is enough; there is no in-place mutation to track.
  const [activeTenantId, setCurrentTenant] = useState(() => getActiveTenantId());

  const refreshAuth = useCallback(async (silent = false) => {
    setCheckingAuth(true);
    setAuthCheckFailed(false);
    try {
      const status = await api<AuthStatus>("/api/auth/status");
      if (status.active_tenant_id) {
        setActiveTenantId(status.active_tenant_id);
        setCurrentTenant(status.active_tenant_id);
      }
      setAuth(status);
      return status;
    } catch (e: any) {
      setAuthCheckFailed(true);
      // The backend shares one event loop with the evolution cycle; under
      // load a quick auth-status check can time out.  Don't scare the user
      // with a toast on the initial page load — only surface the error when
      // they explicitly click "刷新登录信息".
      if (!silent) {
        const isTimeout = e?.status === undefined && /超时|Timeout|Abort/i.test(e?.message || "");
        toastErr(isTimeout ? "登录状态检查超时，服务可能繁忙" : "登录状态检查失败", e.message);
      }
      return null;
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  useEffect(() => {
    refreshAuth(true);
  }, [refreshAuth]);

  useEffect(() => {
    const refresh = () => void refreshAuth(true);
    window.addEventListener("console-auth-changed", refresh);
    window.addEventListener("focus", refresh);
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      window.removeEventListener("console-auth-changed", refresh);
      window.removeEventListener("focus", refresh);
      window.clearInterval(timer);
    };
  }, [refreshAuth]);

  useEffect(() => {
    // A failed request says nothing about the session. Keep the restoration
    // gate open and retry; only a successful status response can show login.
    if (auth || checkingAuth || !authCheckFailed) return;
    const timer = window.setTimeout(() => void refreshAuth(true), 5000);
    return () => window.clearTimeout(timer);
  }, [auth, checkingAuth, authCheckFailed, refreshAuth]);

  const applySharingConfig = useCallback((config: SharingConfig) => {
    setOpenVikingConfigured(hasOpenVikingConfiguration(config));
  }, []);

  useEffect(() => {
    if (!auth?.authenticated || !auth.active_tenant_id) {
      setOpenVikingConfigured(false);
      setOpenVikingConfigChecked(false);
      return;
    }
    let cancelled = false;
    api<SharingConfig>("/api/sharing-config")
      .then((config) => {
        if (!cancelled) {
          applySharingConfig(config);
          setOpenVikingConfigChecked(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setOpenVikingConfigured(false);
          setOpenVikingConfigChecked(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [auth?.authenticated, auth?.active_tenant_id, applySharingConfig]);

  // Tenant header injection is only valid for admin console sessions; keep
  // the client-side gate in sync with the authenticated identity.
  const isAdmin = auth?.user?.role === "admin";
  const isPlatform = auth?.user?.platform_role === "platform_admin";
  const hasTenant = !!auth?.active_tenant_id;
  const tenants = auth?.available_tenants || [];
  const canView = (key: ViewKey) => {
    const item = NAV_SECTIONS.flatMap(s => s.items).find(i => i.key === key);
    if (item?.platformOnly) return isPlatform;
    return hasTenant && (!item?.adminOnly || isAdmin);
  };
  // Set the gate DURING RENDER (not in an effect): child view effects fire
  // before parent effects, so an effect-based flip happened only AFTER the
  // dashboard's first fetch — briefly sending it without X-Tenant-Id and
  // flashing the default tenant's data right after a tenant switch/reload.
  // While auth is still unknown we render the full-screen gate and no view
  // is mounted, so setting it here is both timely and safe.
  setTenantHeaderAllowed(!!auth?.authenticated);

  // Switching tenants remounts the whole app: every view caches per-tenant
  // state fetched before the switch, so a reload is the only sound reset.
  function switchTenant(tenantId: string) {
    if (tenantId === activeTenantId) return;
    setActiveTenantId(tenantId);
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    url.searchParams.set("tab", dashTab);
    window.location.assign(url.toString());
  }

  async function refreshLoginInfo() {
    setUserMenuOpen(false);
    setRefreshingLogin(true);
    const status = await refreshAuth();
    setRefreshingLogin(false);
    if (status?.authenticated) {
      toastOk(
        "登录信息已刷新",
        status.user?.display_name || status.user?.id || ""
      );
    }
  }

  async function logout() {
    setUserMenuOpen(false);
    setLoggingOut(true);
    toastOk("正在退出登录…");
    try {
      await api("/api/auth/logout", { method: "POST" });
      setAuth((previous) => ({ ...previous, authenticated: false, needs_setup: false, user: null }));
      toastOk("已退出登录");
    } catch (e: any) {
      // Even if the logout request times out, clear the local session so
      // the user is not stuck on a wedged button.
      setAuth((previous) => ({ ...previous, authenticated: false, needs_setup: false, user: null }));
      const isTimeout = e?.status === undefined && /超时|Timeout|Abort/i.test(e?.message || "");
      toastErr(isTimeout ? "退出超时，已本地清除登录态" : "退出失败", e.message);
    } finally {
      setLoggingOut(false);
    }
  }

  if (!auth) {
    return (
      <div className="grid min-h-screen place-items-center bg-background text-sm text-muted-foreground">
        <div className="flex flex-col items-center gap-4" role="status">
          {checkingAuth ? "正在检查登录状态…" : (
            <>
              <p>暂时无法确认登录状态，正在自动重试…</p>
              <Button variant="outline" onClick={() => void refreshAuth(true)}>重试</Button>
            </>
          )}
        </div>
        <Toaster position="bottom-right" />
      </div>
    );
  }

  if (!auth?.authenticated) {
    return (
      <>
        <LoginGate
          needsSetup={!!auth?.needs_setup}
          registrationEnabled={auth.self_registration_enabled === true}
          onAuthed={() => { setAuth(null); void refreshAuth(); }}
        />
        <Toaster position="bottom-right" />
      </>
    );
  }

  return (
    <div className="app-shell flex h-screen overflow-hidden">
      <aside className="app-sidebar flex h-screen shrink-0 flex-col border-r">
        <div className="sidebar-brand flex h-[60px] items-center gap-2.5 border-b border-sidebar-border px-[18px]">
          <div
            className="grid size-[31px] shrink-0 place-items-center rounded-[10px] bg-accent text-[12px] font-extrabold tracking-tighter text-white shadow-[0_7px_16px_rgba(15,118,110,0.18)]"
            aria-label="KEE"
            title="KEE"
          >
            KEE
          </div>
          <div className="sidebar-brand-copy">
            <div className="text-[14px] font-[800] leading-tight">KEE</div>
            <div className="mt-0.5 text-[9.5px] font-[600] text-muted-soft">知识进化平台</div>
          </div>
        </div>
        <label className="px-4 py-2 text-xs">工作视角（不改变权限）
          <select aria-label="工作视角" className="mt-1 w-full rounded border p-1" value={perspective} onChange={e => setPerspective(e.target.value)}>
            <option value="agent-manager">智能体与能力负责人</option>
            <option value="knowledge-manager">知识运营负责人</option>
            <option value="research">研究与实验</option>
          </select>
        </label>
        {tenants.length > 0 && (
          <TenantSwitcher
            tenants={tenants}
            activeTenantId={activeTenantId}
            onSelect={switchTenant}
          />
        )}
        <nav className="sidebar-nav flex flex-1 flex-col gap-0.5 overflow-auto px-2.5 py-3" aria-label="主导航">
          {[...NAV_SECTIONS].sort((a, b) => {
            const focus = perspective === "agent-manager" ? "agents" : perspective === "research" ? "evolution" : "knowledge";
            return Number(b.id === focus) - Number(a.id === focus);
          }).map(({ id, label, items }) => {
            const visibleItems = items.filter(({ key }) => canView(key));
            if (!visibleItems.length) return null;
            return (
              <section key={id} className="sidebar-nav-section mb-1.5" aria-labelledby={`nav-section-${id}`}>
                <div
                  id={`nav-section-${id}`}
                  className="sidebar-group-label px-3 pb-1 pt-2.5 text-[10px] font-[800] uppercase tracking-[0.08em] text-muted-soft"
                >
                  {label}
                </div>
                {visibleItems.map(({ key, label: itemLabel, icon: Icon, requiresOpenViking }) => {
                  // OpenViking-backed entries stay visible when it is not
                  // configured: they carry a badge and their page explains how
                  // to enable it, instead of silently disappearing.
                  const notConfigured =
                    !!requiresOpenViking && openVikingConfigChecked && !openVikingConfigured;
                  return (
                    <button
                      key={key}
                      type="button"
                      aria-current={view === key ? "page" : undefined}
                      aria-label={notConfigured ? `${itemLabel}（未配置 OpenViking）` : undefined}
                      onClick={() => {
                        setView(key);
                        setUserMenuOpen(false);
                      }}
                      title={notConfigured ? `${itemLabel}：需先在运行状态中配置 OpenViking` : itemLabel}
                      className={cn(
                        "sidebar-nav-item flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2.5 text-[13px] font-[700] transition-[background-color,color,transform,box-shadow]",
                        view === key
                          ? "bg-sidebar-primary text-sidebar-primary-foreground shadow-[0_7px_18px_rgba(24,24,26,0.12)]"
                          : "text-sidebar-foreground hover:translate-x-[3px] hover:bg-sidebar-accent hover:text-foreground"
                      )}
                    >
                      <Icon className="size-4 shrink-0 opacity-85" />
                      <span className="sidebar-item-label">{itemLabel}</span>
                      {notConfigured && (
                        <span
                          aria-hidden="true"
                          className="ml-auto shrink-0 rounded-full bg-amber-400/25 px-1.5 py-0.5 text-[9.5px] font-bold text-amber-700"
                        >
                          未配置
                        </span>
                      )}
                    </button>
                  );
                })}
              </section>
            );
          })}
        </nav>
        <UserMenu
          user={auth.user}
          open={userMenuOpen}
          onToggle={() => setUserMenuOpen((v) => !v)}
          refreshing={refreshingLogin}
          loggingOut={loggingOut}
          onRefresh={refreshLoginInfo}
          onLogout={logout}
        />
      </aside>

      {/* ---- Content ---- */}
      <main className="app-main h-screen min-w-0 flex-1 overflow-auto">
        {!canView(view) && <div className="m-6 rounded-xl border p-6">
          <p className="font-semibold">{hasTenant ? "当前身份无权管理此页面" : "尚未进入可访问的租户"}</p>
          <p className="mt-2 text-sm text-muted-foreground">{hasTenant ? "请从左侧选择可用页面。" : "请从左侧选择所属租户；若没有可选租户，请联系租户管理员添加成员关系。你的账号 ID：" + auth.user?.id}</p>
          <Button className="mt-4" variant="outline" onClick={() => void refreshAuth()}>刷新权限</Button>
        </div>}
        {MINE_PAGES.map(({ key, page }) => (
          canView(key) && <div key={key} className={cn(view !== key && "hidden")}>
            <MiningView
              active={view === key}
              page={page}
              user={auth.user}
              preferredInputDir={mineInputDir}
              onInputDirChange={setMineInputDir}
              onNavigate={(destination) => {
                if (destination === "candidates") {
                  setDashTab("candidates");
                  setView("dashboard");
                  return;
                }
                if (destination === "skills") {
                  setView("workspace");
                  return;
                }
                setView(`mine-${destination}` as ViewKey);
              }}
            />
          </div>
        ))}
        {canView("dashboard") && view === "dashboard" && <div>
          <PageHeader
            title={DASH_PAGE_META[dashTab].title}
            description={DASH_PAGE_META[dashTab].description}
            badge="进化闭环"
          />
          {/* Operational detail views for the evolution loop. */}
          <div className="section-tabs pt-2.5">
            <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="进化运行页面">
              {DASH_TABS.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={dashTab === key}
                  onClick={() => setDashTab(key)}
                  className={cn(
                    "flex items-center gap-1.5 border-b-2 px-3 py-2 text-[12px] font-[700] transition-colors",
                    dashTab === key
                      ? "border-accent text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  )}
                >
                  <Icon className="size-4 opacity-80" />
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className={cn(dashTab !== "overview" && "hidden")}>
            <DashboardView
              active={view === "dashboard" && dashTab === "overview"}
              onNavigate={(next) => setView(next as ViewKey)}
            />
          </div>
          <div className={cn(dashTab !== "candidates" && "hidden")}>
            <CandidateReviewView active={view === "dashboard" && dashTab === "candidates"} canManage={isAdmin} />
          </div>
          <div className={cn(dashTab !== "audit" && "hidden")}>
            <AuditView active={view === "dashboard" && dashTab === "audit"} />
          </div>
          <div className={cn(dashTab !== "filter" && "hidden")}>
            <SessionFilterView active={view === "dashboard" && dashTab === "filter"} />
          </div>
        </div>}
        {STANDALONE_PAGES.filter(({ key }) => canView(key) && view === key).map(({ key, title, description, badge, scope, render }) => {
          const active = view === key;
          return (
            <div key={`${key}:${activeTenantId}:${auth.user?.id}:${isAdmin}:${isPlatform}`} className={cn(!active && "hidden")}>
              <PageHeader title={title} description={description} badge={badge} scope={scope} />
              {render({
                active,
                user: auth.user,
                openVikingConfigured,
                openVikingConfigChecked,
                onSharingConfigChange: applySharingConfig,
                onNavigate: setView,
                activeTenantId,
              })}
            </div>
          );
        })}
      </main>

      <Toaster position="bottom-right" />
    </div>
  );
}

function OpenVikingRequiredNotice({ onConfigure }: { onConfigure: () => void }) {
  return (
    <div className="mx-auto max-w-[760px] px-[22px] py-10">
      <div className="rounded-[14px] border border-amber-300 bg-amber-50 p-5">
        <div className="text-[15px] font-bold text-amber-900">该功能需要先连接 OpenViking</div>
        <p className="mt-2 text-[13px] leading-relaxed text-amber-800">
          Agent 工作空间与平台资产读取 OpenViking 中的团队 Skills、Memory 和资源。
          当前尚未配置 OpenViking 服务凭据，或连接暂不可用。
        </p>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-[12.5px] leading-relaxed text-amber-800">
          <li>前往「项目配置 → 运行状态」的 OpenViking 部署区域；</li>
          <li>选择部署方式（火山云 / 自建）并填写 Endpoint、Service Key；</li>
          <li>保存后返回本页，即可浏览与编辑资产。</li>
        </ol>
        <Button className="mt-4" size="sm" onClick={onConfigure}>
          前往运行状态配置
        </Button>
      </div>
    </div>
  );
}

function LoginGate({
  needsSetup,
  registrationEnabled,
  onAuthed,
}: {
  needsSetup: boolean;
  registrationEnabled: boolean;
  onAuthed: (status: AuthStatus) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState(needsSetup ? "admin" : "");
  const [displayName, setDisplayName] = useState(needsSetup ? "admin" : "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState(needsSetup ? "admin" : "");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const isRegister = !needsSetup && registrationEnabled && mode === "register";

  async function submit() {
    if (isRegister && password !== confirmPassword) {
      toastErr("注册失败", "两次输入的密码不一致");
      return;
    }
    setLoading(true);
    try {
      const path = needsSetup ? "/api/auth/bootstrap" : isRegister ? "/api/auth/register" : "/api/auth/login";
      const payload = needsSetup
        ? { username, display_name: displayName || username, email, password }
        : isRegister
          ? { username, display_name: displayName || username, email, password }
          : { username, password };
      const status = await api<AuthStatus>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onAuthed(status);
      toastOk(needsSetup ? "管理员已初始化" : isRegister ? "注册成功" : "登录成功", status.user?.display_name || status.user?.id || "");
    } catch (e: any) {
      toastErr(needsSetup ? "初始化失败" : isRegister ? "注册失败" : "登录失败",
        e.message === "SELF_REGISTRATION_DISABLED" ? "当前不开放自主注册，请联系管理员开通账号。" : e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="grid min-h-screen bg-background lg:grid-cols-[1.15fr_1fr]">
      {/* ---- Left: animated carousel showcase ---- */}
      <LoginHero />

      {/* ---- Right: auth form ---- */}
      <div className="grid place-items-center bg-transparent px-6 py-10">
        <div className="w-full max-w-[420px]">
          <div className="mb-5 lg:hidden">
            <span className="inline-flex items-center gap-2 rounded-full bg-accent-soft px-3 py-1 text-xs font-semibold text-accent">
              <Sparkles className="size-3.5" /> KEE · 知识进化平台
            </span>
          </div>
          <div className="rounded-[18px] border border-border bg-surface p-6 shadow-[var(--shadow-float)]">
            <div className="mb-5">
              <div
                className="mb-2 grid size-10 place-items-center rounded-xl bg-accent text-sm font-extrabold text-white shadow-[0_8px_18px_rgba(15,118,110,0.18)]"
                aria-label="KEE"
                title="KEE"
              >
                KEE
              </div>
              <h1 className="text-[22px] font-bold tracking-tight">
                {needsSetup ? "初始化管理员账号" : isRegister ? "注册 KEE 账号" : "登录 KEE 控制台"}
              </h1>
              <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
                {needsSetup
                  ? "当前还没有用户。默认管理员账号和密码均为 admin，可直接创建后登录。"
                  : isRegister
                    ? "注册后请联系租户管理员添加成员关系，即可进入所属租户。"
                    : "请输入账号密码后继续访问团队技能进化控制台。"}
              </p>
            </div>

            <div className="space-y-3.5">
              {!needsSetup && !registrationEnabled && (
                <p className="text-sm text-muted-foreground">当前不开放自主注册，请联系管理员开通账号。</p>
              )}
              {!needsSetup && registrationEnabled && (
                <div className="grid grid-cols-2 rounded-lg border border-border bg-background p-1 text-xs font-semibold">
                  <button
                    type="button"
                    onClick={() => setMode("login")}
                    className={cn(
                      "rounded-md px-3 py-2 transition-colors",
                      !isRegister ? "bg-sidebar-primary text-white" : "text-muted-foreground hover:bg-muted"
                    )}
                  >
                    账号登录
                  </button>
                  <button
                    type="button"
                    onClick={() => setMode("register")}
                    className={cn(
                      "rounded-md px-3 py-2 transition-colors",
                      isRegister ? "bg-sidebar-primary text-white" : "text-muted-foreground hover:bg-muted"
                    )}
                  >
                    新用户注册
                  </button>
                </div>
              )}
              <Field label="账号">
                <Input value={username} placeholder="admin" onChange={(e) => setUsername(e.target.value)} />
              </Field>
              {(needsSetup || isRegister) && (
                <div className="grid gap-3.5 sm:grid-cols-2">
                  <Field label="显示名">
                    <Input value={displayName} placeholder="管理员" onChange={(e) => setDisplayName(e.target.value)} />
                  </Field>
                  <Field label="邮箱">
                    <Input value={email} placeholder="name@example.com" onChange={(e) => setEmail(e.target.value)} />
                  </Field>
                </div>
              )}
              <Field label="密码">
                <Input
                  type="password"
                  value={password}
                  placeholder={needsSetup ? "默认 admin" : "请输入密码"}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submit();
                  }}
                />
              </Field>
              {isRegister && (
                <Field label="确认密码">
                  <Input
                    type="password"
                    value={confirmPassword}
                    placeholder="请再次输入密码"
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") submit();
                    }}
                  />
                </Field>
              )}
              <Button className="w-full" disabled={loading} onClick={submit}>
                {needsSetup ? "创建管理员并登录" : isRegister ? "注册并登录" : "登录"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const LOGIN_SLIDES: {
  icon: typeof Sparkles;
  tag: string;
  title: string;
  desc: string;
  points: string[];
}[] = [
  {
    icon: Clock,
    tag: "把重复劳动交给技能",
    title: "别再一遍遍写同样的流程",
    desc: "请假审批、周报汇总、小红书选题、翻译润色……这些反复出现的活儿，teamEvolver 把它们沉淀成可复用的“技能”，下次一键调用。",
    points: ["高频事务自动成型", "团队共享同一套最佳做法", "新人开箱即用"],
  },
  {
    icon: TrendingUp,
    tag: "越用越聪明",
    title: "从每一次真实对话里进化",
    desc: "平台会从大家的日常会话中自动发现有价值的做法，提炼为候选技能，经真实回放 A/B 评估后再上线——好用的留下，不好用的淘汰。",
    points: ["自动挖掘高价值会话", "真实回放打分对比", "择优上线、持续迭代"],
  },
  {
    icon: ShieldCheck,
    tag: "放心用、可追溯",
    title: "有评审、有版本、可回滚",
    desc: "每个技能都有版本历史与审计记录，管理员可随时查看差异、回滚到任意版本。上线前人工评审把关，用得安心。",
    points: ["候选评审人工把关", "完整版本与审计链", "一键回滚任意版本"],
  },
  {
    icon: Zap,
    tag: "为白领而生",
    title: "让每个人都像团队里的老手",
    desc: "无需懂技术，打开控制台就能用同事验证过的成熟技能处理日常工作，把时间还给真正重要的事。",
    points: ["零门槛上手", "沉淀团队集体经验", "专注高价值创造"],
  },
];

const LOGIN_CHIPS = [
  "请假审批流程",
  "周报自动汇总",
  "会议纪要整理",
  "小红书选题增长",
  "多语翻译润色",
  "日程 & 待办摘要",
  "面试评估",
  "数据治理巡检",
];

function LoginHero() {
  const [idx, setIdx] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setIdx((v) => (v + 1) % LOGIN_SLIDES.length), 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="login-hero relative hidden flex-col justify-between p-10 lg:flex xl:p-14">
      {/* animated backdrop */}
      <div className="login-blob" style={{ width: 320, height: 320, top: -60, right: -40, background: "rgba(126, 231, 213, 0.55)" }} />
      <div className="login-blob" style={{ width: 260, height: 260, bottom: -40, left: -30, background: "rgba(132, 115, 194, 0.48)", animationDelay: "-6s" }} />
      <div className="login-grid" />

      {/* brand */}
      <div className="relative z-10 flex items-center gap-3">
        <div
          className="grid size-11 place-items-center rounded-2xl bg-white/15 text-base font-extrabold tracking-tighter ring-1 ring-white/25 backdrop-blur"
          aria-label="KEE"
          title="KEE"
        >
          TE
        </div>
        <div>
          <div className="text-[17px] font-bold tracking-tight">KEE</div>
          <div className="text-xs text-white/60">团队技能进化平台</div>
        </div>
      </div>

      {/* carousel */}
      <div className="relative z-10 my-8 min-h-[290px]">
        {LOGIN_SLIDES.map((s, i) => {
          const Icon = s.icon;
          return (
            <div key={i} className="login-slide" data-active={i === idx}>
              <span className="inline-flex items-center gap-2 rounded-full bg-white/12 px-3 py-1 text-xs font-semibold text-white/85 ring-1 ring-white/20">
                <Icon className="size-3.5" /> {s.tag}
              </span>
              <h2 className="mt-5 text-[30px] font-bold leading-[1.2] tracking-tight xl:text-[34px]">
                {s.title}
              </h2>
              <p className="mt-4 max-w-[440px] text-[15px] leading-relaxed text-white/75">
                {s.desc}
              </p>
              <ul className="mt-6 space-y-2.5">
                {s.points.map((p) => (
                  <li key={p} className="flex items-center gap-2.5 text-sm text-white/85">
                    <span className="grid size-5 shrink-0 place-items-center rounded-full bg-white/15 ring-1 ring-white/25">
                      <Sparkles className="size-3" />
                    </span>
                    {p}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>

      {/* controls + marquee */}
      <div className="relative z-10 space-y-6">
        <div className="flex items-center gap-2">
          {LOGIN_SLIDES.map((_, i) => (
            <button
              key={i}
              type="button"
              aria-label={`第 ${i + 1} 页`}
              onClick={() => setIdx(i)}
              className="login-dot"
              style={{ width: i === idx ? 34 : 16 }}
            >
              {i === idx && <span key={idx} className="fill" />}
            </button>
          ))}
        </div>

        <div className="login-marquee-mask">
          <div className="login-marquee">
            {[...LOGIN_CHIPS, ...LOGIN_CHIPS].map((c, i) => (
              <span key={i} className="login-chip">
                <Repeat2 className="size-3.5 opacity-70" />
                {c}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function TenantSwitcher({
  tenants,
  activeTenantId,
  onSelect,
  projectMode = false,
}: {
  tenants: TenantsResp["tenants"];
  activeTenantId: string;
  onSelect: (tenantId: string) => void;
  projectMode?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const entries = tenants.filter(t => t.status === "active");
  const activeTenant =
    entries.find((t) => t.tenant_id === activeTenantId) ||
    { tenant_id: "", display_name: "请选择租户", status: "active" };
  return (
    <div className={cn("relative", projectMode ? "w-full" : "border-b border-sidebar-border p-2.5")}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`${projectMode ? "项目" : "租户"}切换：当前 ${activeTenant.display_name}`}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2.5 rounded-[12px] border px-2.5 py-2 text-left transition-colors",
          !projectMode && "sidebar-tenant-button",
          open ? "border-accent/30 bg-accent-soft" : "border-transparent hover:border-border hover:bg-muted"
        )}
      >
        <span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-accent-soft text-accent">
          <Building2 className="size-4" />
        </span>
        <span className={cn("min-w-0 flex-1", !projectMode && "sidebar-tenant-copy")}>
          <span className="block text-[10px] font-[600] uppercase tracking-wide text-muted-soft">{projectMode ? "当前项目" : "当前租户"}</span>
          <span className="block truncate text-[13px] font-semibold">
            {activeTenant.display_name || activeTenant.tenant_id}
          </span>
        </span>
        <ChevronsUpDown className={cn("size-3.5 shrink-0 text-muted-soft", !projectMode && "sidebar-tenant-chevron")} />
      </button>
      {open && (
        <div
          role="menu"
          className="sidebar-tenant-popover absolute left-0 right-0 top-[calc(100%+8px)] z-50 max-h-80 overflow-y-auto rounded-md border border-border bg-surface shadow-[var(--shadow-float)]"
        >
          <div className="p-2"><Input aria-label="搜索项目" placeholder="搜索项目" value={search} onChange={e => setSearch(e.target.value)} /></div>
          {!entries.some(t => t.status === "active" && `${t.display_name} ${t.tenant_id}`.toLowerCase().includes(search.trim().toLowerCase())) && <div role="status" className="p-3 text-xs text-muted-foreground">无匹配项目</div>}
          {entries.filter(t => t.status === "active" && `${t.display_name} ${t.tenant_id}`.toLowerCase().includes(search.trim().toLowerCase())).map((t) => (
            <button
              key={t.tenant_id}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onSelect(t.tenant_id);
              }}
              className={cn(
                "flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left text-sm hover:bg-muted",
                t.tenant_id === activeTenant.tenant_id && "bg-accent-soft font-semibold"
              )}
            >
              <span className="min-w-0 flex-1 truncate">
                {t.display_name || t.tenant_id}
                <span className="ml-1.5 text-[11px] text-muted-foreground">{t.tenant_id}</span>
              </span>
              {t.status !== "active" && <span className="text-[10px] text-muted-soft">已禁用</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function UserMenu({
  user,
  open,
  refreshing,
  loggingOut,
  onToggle,
  onRefresh,
  onLogout,
}: {
  user?: UserProfile | null;
  open: boolean;
  refreshing: boolean;
  loggingOut: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onLogout: () => void;
}) {
  const name = user?.display_name || user?.id || "unknown";
  const initials = name.slice(0, 1).toUpperCase();
  return (
    <div className="sidebar-user-footer relative border-t border-sidebar-border p-2.5">
      <div className="sidebar-version px-2 pb-2 text-[10px] font-[600] text-muted-soft">统一控制台 · v1</div>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`账户菜单：${name}`}
        onClick={onToggle}
        className={cn(
          "sidebar-user-button flex w-full items-center gap-2.5 rounded-[12px] border px-2.5 py-2 text-left transition-colors",
          open ? "border-accent/30 bg-accent-soft" : "border-transparent hover:border-border hover:bg-muted"
        )}
      >
        <span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-accent text-xs font-bold text-white shadow-sm">
          {initials}
        </span>
        <span className="sidebar-user-copy min-w-0 flex-1">
          <span className="block truncate text-[13px] font-semibold">{name}</span>
          <span className="block text-[10.5px] text-muted-foreground">{user?.platform_role === "platform_admin" ? "平台超级管理员" : user?.tenant_role === "tenant_admin" ? "租户管理员" : "普通成员"}</span>
        </span>
        <ChevronsUpDown className="sidebar-user-chevron size-3.5 shrink-0 text-muted-soft" />
      </button>
      {open && (
        <div role="menu" className="sidebar-user-popover absolute bottom-[calc(100%+8px)] left-2.5 right-2.5 z-50 overflow-hidden rounded-[14px] border border-border bg-surface shadow-[var(--shadow-float)]">
          <div className="border-b border-line px-4 py-3">
            <div className="text-sm font-bold">{name}</div>
            <div className="mt-1 text-xs text-muted-foreground">{user?.email || user?.id || ""}</div>
          </div>
          <button
            type="button"
            role="menuitem"
            disabled={refreshing || loggingOut}
            onClick={onRefresh}
            className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className={refreshing ? "size-4 animate-spin" : "size-4"} />
            {refreshing ? "刷新中…" : "刷新登录信息"}
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={refreshing || loggingOut}
            onClick={onLogout}
            className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-destructive hover:bg-muted disabled:opacity-50"
          >
            <LogOut className="size-4" />
            {loggingOut ? "退出中…" : "退出登录"}
          </button>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
