// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  customerMode: true,
  openVikingConfigured: true,
  tenantId: "default",
  role: "admin" as "admin" | "user",
  platform: true,
  membership: true,
}));
vi.mock("@/api/client", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/sharing-config") {
      return {
        endpoint: "http://openviking.test",
        api_key_present: state.openVikingConfigured,
      };
    }
    return {
      authenticated: true,
      needs_setup: false,
      customer_mode: state.customerMode,
      active_tenant_id: state.membership ? state.tenantId : null,
      available_tenants: state.membership ? [{ tenant_id: state.tenantId, display_name: state.tenantId, status: "active" }] : [],
      user: { id: state.role, display_name: state.role === "admin" ? "Admin" : "User", role: state.role,
        platform_role: state.platform ? "platform_admin" : "user", tenant_role: state.role === "admin" ? "tenant_admin" : "member" },
    };
  }),
  getActiveTenantId: () => state.tenantId,
  setActiveTenantId: vi.fn(), setTenantHeaderAllowed: vi.fn(),
  listTenants: vi.fn(async () => ({ mode: "postgres", tenants: [
    { tenant_id: "default", display_name: "Default", status: "active" },
    { tenant_id: "account-a", display_name: "Account A", status: "active" },
  ] })),
}));
vi.mock("@/views/DashboardView", () => ({ default: () => <div>dashboard-body</div> }));
vi.mock("@/views/UsersView", () => ({ default: () => <div>users-body</div> }));
vi.mock("@/views/MembersView", () => ({ default: () => <div>members-body</div> }));
vi.mock("@/views/ModelSettingsView", () => ({ default: () => <div>model-body</div> }));
vi.mock("@/views/CandidateReviewView", () => ({ default: () => <div>review-body</div> }));
vi.mock("@/views/HealthView", () => ({
  default: ({
    onSharingConfigChange,
  }: {
    onSharingConfigChange?: (config: {
      endpoint: string;
      api_key_present: boolean;
    }) => void;
  }) => (
    <>
      <div>health-body</div>
      <button
        type="button"
        onClick={() => onSharingConfigChange?.({
          endpoint: "http://openviking.test",
          api_key_present: true,
        })}
      >
        configure-openviking
      </button>
    </>
  ),
}));
vi.mock("@/views/AuditView", () => ({ default: () => <div>audit-body</div> }));
vi.mock("@/views/ExperienceLibraryView", () => ({ default: () => <div>experience-library-body</div> }));
vi.mock("@/views/SessionFilterView", () => ({ default: () => <div>filter-body</div> }));
vi.mock("@/views/DataSourcesView", () => ({ default: () => <div>datasource-body</div> }));
vi.mock("@/views/ObservabilityView", () => ({ default: () => <div>observability-body</div> }));
vi.mock("@/views/EvolutionWorkspaceView", () => ({
  default: ({ openVikingConfigured }: { openVikingConfigured: boolean }) => (
    <div>evolution-body-{String(openVikingConfigured)}</div>
  ),
}));
vi.mock("@/views/OpenVikingWorkspaceShell", () => ({
  default: () => <div>agent-assets-body</div>,
}));
vi.mock("@/views/PlatformAssetsView", () => ({
  default: () => <div>platform-assets-body</div>,
}));
vi.mock("@/views/SkillWorkbenchView", () => ({
  default: () => <div>skill-workbench-body</div>,
}));
vi.mock("@/views/TenantsView", () => ({ default: () => <div>tenants-body</div> }));
import App from "@/App";
import { api } from "@/api/client";

beforeEach(() => {
  state.customerMode = true;
  state.openVikingConfigured = true;
  state.tenantId = "default";
  state.role = "admin";
  state.platform = true;
  state.membership = true;
  window.history.replaceState(null, "", "/");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("login restoration", () => {
  it.each([false, undefined])("hides public registration when not explicitly enabled: %s", async (enabled) => {
    vi.mocked(api).mockResolvedValueOnce({
      authenticated: false, needs_setup: false, self_registration_enabled: enabled,
    });
    render(<App />);
    expect(await screen.findByRole("button", { name: "登录" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "新用户注册" })).not.toBeInTheDocument();
    expect(screen.getByText("当前不开放自主注册，请联系管理员开通账号。")).toBeVisible();
    fireEvent.change(screen.getByPlaceholderText("admin"), { target: { value: "alice" } });
    fireEvent.change(screen.getByPlaceholderText("请输入密码"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("navigation", { name: "主导航" })).toBeVisible();
    expect(api).toHaveBeenCalledWith("/api/auth/login", expect.objectContaining({ method: "POST" }));
  });

  it("shows the registration form when enabled", async () => {
    vi.mocked(api).mockResolvedValueOnce({
      authenticated: false, needs_setup: false, self_registration_enabled: true,
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "新用户注册" }));
    expect(screen.getByRole("button", { name: "注册并登录" })).toBeVisible();
    expect(screen.getByPlaceholderText("请再次输入密码")).toBeVisible();
  });

  it.each([
    new Error("请求超时（60s），稍后自动重试"),
    Object.assign(new Error("service busy"), { status: 503 }),
  ])("allows retrying a failed status check without showing the login form: %s", async (error) => {
    vi.mocked(api).mockRejectedValueOnce(error);
    render(<App />);

    const retry = await screen.findByRole("button", { name: "重试" });
    expect(screen.queryByPlaceholderText("请输入密码")).not.toBeInTheDocument();
    fireEvent.click(retry);
    expect(await screen.findByRole("navigation", { name: "主导航" })).toBeVisible();
  });

  it("automatically restores login when the status endpoint recovers", async () => {
    vi.useFakeTimers();
    vi.mocked(api).mockRejectedValueOnce(new Error("Failed to fetch"));
    await act(async () => { render(<App />); });
    expect(screen.getByRole("button", { name: "重试" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeVisible();
  });

  it("shows login when the server confirms the session is absent", async () => {
    vi.mocked(api).mockResolvedValueOnce({ authenticated: false, needs_setup: false });
    render(<App />);
    expect(await screen.findByPlaceholderText("请输入密码")).toBeVisible();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });
});

describe("original console functionality", () => {
  it.each([false, true])("keeps every original entry with customer_mode=%s", async (mode) => {
    state.customerMode = mode;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    await within(navigation).findByRole("button", { name: "实验工作台" });
    for (const name of ["运行总览", "经验库", "数据源接入", "进化链路",
      "实验工作台", "个人与团队资产", "平台资产", "模型配置", "平台用户", "租户管理", "运行状态"]) {
      expect(within(navigation).getByRole("button", { name })).toBeVisible();
    }
    await screen.findByRole("button", { name: /租户切换/ });
    expect(document.querySelector("aside")?.textContent).toContain("当前租户");
    expect(document.querySelector("main > header")).toBeNull();
  });

  it("keeps the workbench focused and opens OpenViking assets from the sidebar", async () => {
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    fireEvent.click(within(navigation).getByRole("button", { name: "实验工作台" }));
    expect(await screen.findByText("skill-workbench-body")).toBeVisible();
    expect(screen.queryByText("skill-lab-body")).toBeNull();
    expect(screen.queryByText("memory-lab-body")).toBeNull();
    expect(screen.queryByText("team-only-replacement")).toBeNull();
    fireEvent.click(within(navigation).getByRole("button", { name: "个人与团队资产" }));
    expect(await screen.findByText("agent-assets-body")).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: "平台资产" }));
    expect(await screen.findByText("platform-assets-body")).toBeVisible();
  });

  it("opens the experience library from the sidebar", async () => {
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    fireEvent.click(within(navigation).getByRole("button", { name: "经验库" }));
    expect(screen.getByText("experience-library-body")).toBeVisible();
    expect(screen.queryByRole("tab", { name: "经验库" })).not.toBeInTheDocument();
  });

  it("keeps every project configuration page active after navigation", async () => {
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    for (const [label, body] of [
      ["成员与接入", "members-body"],
      ["模型配置", "model-body"],
      ["进化链路", "evolution-body-true"],
      ["数据源接入", "datasource-body"],
      ["运行状态", "health-body"],
    ]) {
      const item = within(navigation).getByRole("button", { name: label });
      fireEvent.click(item);
      expect(await screen.findByText(body)).toBeVisible();
      expect(item).toHaveAttribute("aria-current", "page");
      expect(screen.queryByText("dashboard-body")).not.toBeInTheDocument();
    }
  });

  it("keeps platform assets available without OpenViking", async () => {
    state.openVikingConfigured = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(
      await within(navigation).findByRole("button", { name: "实验工作台" }),
    ).toBeVisible();
    expect(
      within(navigation).getByRole("button", { name: "平台资产" }),
    ).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: "平台资产" }));
    expect(await screen.findByText("platform-assets-body")).toBeVisible();
    expect(within(navigation).getByRole("button", { name: "运行状态" })).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: "进化链路" }));
    expect(await screen.findByText("evolution-body-false")).toBeVisible();
  });

  it("opens the code workspace directly without requiring OpenViking", async () => {
    state.openVikingConfigured = false;
    window.history.replaceState(null, "", "/?view=workspace");
    render(<App />);
    expect(await screen.findByText("skill-workbench-body")).toBeVisible();
    expect(window.location.search).toContain("view=workspace");
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(within(navigation).getByRole("button", { name: "实验工作台" })).toBeVisible();
  });

  it("keeps platform assets independent when OpenViking is configured later", async () => {
    state.openVikingConfigured = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    await within(navigation).findByRole("button", { name: "实验工作台" });

    fireEvent.click(within(navigation).getByRole("button", { name: "运行状态" }));
    fireEvent.click(await screen.findByRole("button", { name: "configure-openviking" }));

    expect(
      await within(navigation).findByRole("button", { name: "实验工作台" }),
    ).toBeVisible();
    expect(within(navigation).getByRole("button", { name: "平台资产" })).toBeVisible();
    expect(within(navigation).queryByText("未配置")).toBeNull();
  });

  it("keeps every admin entry visible outside the default tenant", async () => {
    state.tenantId = "account-a";
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    for (const name of [
      "链路观测",
      "平台用户",
      "租户管理",
      "模型配置",
      "进化链路",
      "数据源接入",
      "运行状态",
    ]) {
      expect(within(navigation).getByRole("button", { name })).toBeVisible();
    }
  });

  it("retains member tools and hides both platform and tenant governance", async () => {
    state.tenantId = "account-a";
    state.role = "user";
    state.platform = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getByRole("button", { name: "运行总览" })).toBeVisible();
    expect(within(navigation).getByRole("button", { name: "个人与团队资产" })).toBeVisible();
    for (const name of ["链路观测", "平台用户", "租户管理", "模型配置", "进化链路", "数据源接入", "运行状态"]) {
      expect(within(navigation).queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("shows platform controls without granting tenant governance", async () => {
    state.role = "user";
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getByRole("button", { name: "平台用户" })).toBeVisible();
    expect(within(navigation).queryByRole("button", { name: "成员与接入" })).not.toBeInTheDocument();
    expect(screen.queryByText("支持模式")).not.toBeInTheDocument();
  });

  it("shows tenant governance without granting platform controls", async () => {
    state.platform = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getByRole("button", { name: "成员与接入" })).toBeVisible();
    expect(within(navigation).queryByRole("button", { name: "平台用户" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("支持原因")).not.toBeInTheDocument();
  });

  it("does not mount business views without membership", async () => {
    state.membership = false;
    state.platform = false;
    state.role = "user";
    render(<App />);
    expect(await screen.findByText("尚未进入可访问的租户")).toBeVisible();
    expect(screen.queryByText("dashboard-body")).not.toBeInTheDocument();
    expect(screen.queryByText("skill-workbench-body")).not.toBeInTheDocument();
  });
});
