import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Toaster } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { Activity, ClipboardCheck, Filter, History, LayoutDashboard, BookOpenText, Users, SlidersHorizontal, LogOut, RefreshCw, Sparkles, Clock, Repeat2, ShieldCheck, TrendingUp, Zap } from "lucide-react";
import { api, type AuthStatus, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import DashboardView from "@/views/DashboardView";
import SkillsView from "@/views/SkillsView";
import UsersView from "@/views/UsersView";
import ModelSettingsView from "@/views/ModelSettingsView";
import CandidateReviewView from "@/views/CandidateReviewView";
import HealthView from "@/views/HealthView";
import AuditView from "@/views/AuditView";
import SessionFilterView from "@/views/SessionFilterView";

type ViewKey = "dashboard" | "health" | "skills" | "users" | "model";
type DashTab = "overview" | "candidates" | "audit" | "filter";

const NAV: { key: ViewKey; label: string; icon: typeof LayoutDashboard }[] = [
  { key: "dashboard", label: "进化看板", icon: LayoutDashboard },
  { key: "health", label: "系统健康", icon: Activity },
  { key: "skills", label: "技能管理", icon: BookOpenText },
  { key: "users", label: "用户管理", icon: Users },
  { key: "model", label: "模型配置", icon: SlidersHorizontal },
];

// Sub-pages hosted inside the 进化看板 group.
const DASH_TABS: { key: DashTab; label: string; icon: typeof LayoutDashboard }[] = [
  { key: "overview", label: "总览", icon: LayoutDashboard },
  { key: "candidates", label: "候选评审", icon: ClipboardCheck },
  { key: "audit", label: "进化审计", icon: History },
  { key: "filter", label: "过滤审计", icon: Filter },
];

export default function App() {
  const [view, setView] = useState<ViewKey>("dashboard");
  const [dashTab, setDashTab] = useState<DashTab>("overview");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [userMenuOpen, setUserMenuOpen] = useState(false);

  const refreshAuth = useCallback(async () => {
    setCheckingAuth(true);
    try {
      const status = await api<AuthStatus>("/api/auth/status");
      setAuth(status);
    } catch (e: any) {
      setAuth({ authenticated: false, needs_setup: false });
      toastErr("登录状态检查失败", e.message);
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
      setAuth({ authenticated: false, needs_setup: false });
      setUserMenuOpen(false);
      toastOk("已退出登录");
    } catch (e: any) {
      toastErr("退出失败", e.message);
    }
  }

  if (checkingAuth && !auth) {
    return (
      <div className="grid min-h-screen place-items-center bg-background text-sm text-muted-foreground">
        正在检查登录状态…
        <Toaster position="bottom-right" />
      </div>
    );
  }

  if (!auth?.authenticated) {
    return (
      <>
        <LoginGate
          needsSetup={!!auth?.needs_setup}
          onAuthed={(next) => setAuth(next)}
        />
        <Toaster position="bottom-right" />
      </>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* ---- Sidebar (StaffDeck SD1 layout) ---- */}
      <aside className="flex h-screen w-54 shrink-0 flex-col border-r border-line bg-surface">
        <div className="flex h-[58px] items-center gap-2.5 border-b border-line px-[18px]">
          <div className="grid size-[30px] place-items-center rounded-lg bg-sidebar-primary text-[13px] font-extrabold tracking-tighter text-white">
            SG
          </div>
          <div className="text-[15px] font-bold tracking-tight">teamEvolver</div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2.5 py-3">
          {NAV.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setView(key)}
              className={cn(
                "flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13.5px] font-semibold transition-colors",
                view === key
                  ? "bg-sidebar-accent text-foreground"
                  : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-foreground"
              )}
            >
              <Icon className="size-4 opacity-80" />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-auto border-t border-line px-[18px] py-3.5 text-[11px] leading-relaxed text-muted-soft">
          团队技能进化平台
          <br />
          统一控制台 · v1
        </div>
      </aside>

      {/* ---- Content ---- */}
      <main className="h-screen flex-1 overflow-auto bg-background">
        <UserMenu
          user={auth.user}
          open={userMenuOpen}
          onToggle={() => setUserMenuOpen((v) => !v)}
          onRefresh={refreshAuth}
          onLogout={logout}
        />
        <div className={cn(view !== "dashboard" && "hidden")}>
          {/* Sub-tab bar for the 进化看板 group */}
          <div className="border-b border-line bg-surface px-7 pt-4">
            <div className="flex flex-wrap gap-1.5">
              {DASH_TABS.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  onClick={() => setDashTab(key)}
                  className={cn(
                    "flex items-center gap-1.5 rounded-t-lg border-b-2 px-3.5 py-2 text-[13px] font-semibold transition-colors",
                    dashTab === key
                      ? "border-sidebar-primary text-foreground"
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
            <DashboardView active={view === "dashboard" && dashTab === "overview"} />
          </div>
          <div className={cn(dashTab !== "candidates" && "hidden")}>
            <CandidateReviewView active={view === "dashboard" && dashTab === "candidates"} />
          </div>
          <div className={cn(dashTab !== "audit" && "hidden")}>
            <AuditView active={view === "dashboard" && dashTab === "audit"} />
          </div>
          <div className={cn(dashTab !== "filter" && "hidden")}>
            <SessionFilterView active={view === "dashboard" && dashTab === "filter"} />
          </div>
        </div>
        <div className={cn(view !== "health" && "hidden")}>
          <HealthView active={view === "health"} user={auth.user} />
        </div>
        <div className={cn(view !== "skills" && "hidden")}>
          <SkillsView active={view === "skills"} user={auth.user} />
        </div>
        <div className={cn(view !== "users" && "hidden")}>
          <UsersView active={view === "users"} />
        </div>
        <div className={cn(view !== "model" && "hidden")}>
          <ModelSettingsView active={view === "model"} user={auth.user} />
        </div>
      </main>

      <Toaster position="bottom-right" />
    </div>
  );
}

function LoginGate({
  needsSetup,
  onAuthed,
}: {
  needsSetup: boolean;
  onAuthed: (status: AuthStatus) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState(needsSetup ? "admin" : "");
  const [displayName, setDisplayName] = useState(needsSetup ? "admin" : "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState(needsSetup ? "admin" : "");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const isRegister = !needsSetup && mode === "register";

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
      toastErr(needsSetup ? "初始化失败" : isRegister ? "注册失败" : "登录失败", e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.15fr_1fr]">
      {/* ---- Left: animated carousel showcase ---- */}
      <LoginHero />

      {/* ---- Right: auth form ---- */}
      <div className="grid place-items-center bg-background px-6 py-10">
        <div className="w-full max-w-[420px]">
          <div className="mb-5 lg:hidden">
            <span className="inline-flex items-center gap-2 rounded-full bg-accent-soft px-3 py-1 text-xs font-semibold text-accent">
              <Sparkles className="size-3.5" /> teamEvolver · 团队技能进化平台
            </span>
          </div>
          <div className="rounded-4xl border border-border bg-surface p-6 shadow-[var(--shadow-float)]">
            <div className="mb-5">
              <div className="mb-2 grid size-10 place-items-center rounded-xl bg-sidebar-primary text-sm font-extrabold text-white">
                SG
              </div>
              <h1 className="text-[22px] font-bold tracking-tight">
                {needsSetup ? "初始化管理员账号" : isRegister ? "注册 teamEvolver 账号" : "登录 teamEvolver 控制台"}
              </h1>
              <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
                {needsSetup
                  ? "当前还没有用户。默认管理员账号和密码均为 admin，可直接创建后登录。"
                  : isRegister
                    ? "注册后将创建普通用户账号，管理员权限需由管理员在用户管理中分配。"
                    : "请输入账号密码后继续访问团队技能进化控制台。"}
              </p>
            </div>

            <div className="space-y-3.5">
              {!needsSetup && (
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
      <div className="login-blob" style={{ width: 260, height: 260, bottom: -40, left: -30, background: "rgba(198, 120, 70, 0.5)", animationDelay: "-6s" }} />
      <div className="login-grid" />

      {/* brand */}
      <div className="relative z-10 flex items-center gap-3">
        <div className="grid size-11 place-items-center rounded-2xl bg-white/15 text-base font-extrabold tracking-tighter ring-1 ring-white/25 backdrop-blur">
          SG
        </div>
        <div>
          <div className="text-[17px] font-bold tracking-tight">teamEvolver</div>
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

function UserMenu({
  user,
  open,
  onToggle,
  onRefresh,
  onLogout,
}: {
  user?: UserProfile | null;
  open: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onLogout: () => void;
}) {
  const name = user?.display_name || user?.id || "unknown";
  const initials = name.slice(0, 1).toUpperCase();
  return (
    <div className="fixed top-4 right-5 z-50">
      <button
        onClick={onToggle}
        className="flex items-center gap-2 rounded-full border border-border bg-surface px-2 py-1.5 shadow-[var(--shadow-soft)] hover:bg-muted"
      >
        <span className="grid size-7 place-items-center rounded-full bg-sidebar-primary text-xs font-bold text-white">
          {initials}
        </span>
        <span className="max-w-[160px] truncate text-sm font-semibold">{name}</span>
        <span className="text-[11px] text-muted-foreground">{user?.role === "admin" ? "管理员" : "用户"}</span>
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-[240px] overflow-hidden rounded-xl border border-border bg-surface shadow-[var(--shadow-float)]">
          <div className="border-b border-line px-4 py-3">
            <div className="text-sm font-bold">{name}</div>
            <div className="mt-1 text-xs text-muted-foreground">{user?.email || user?.id || ""}</div>
          </div>
          <button onClick={onRefresh} className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm hover:bg-muted">
            <RefreshCw className="size-4" />
            刷新登录信息
          </button>
          <button onClick={onLogout} className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-destructive hover:bg-muted">
            <LogOut className="size-4" />
            退出登录
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
