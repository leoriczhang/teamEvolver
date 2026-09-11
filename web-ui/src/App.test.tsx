// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const state = vi.hoisted(() => ({
  customerMode: true,
  openVikingConfigured: true,
  tenantId: "default",
}));
vi.mock("@/api/client", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/sharing-config") {
      return {
        enabled: state.openVikingConfigured,
        endpoint: "http://openviking.test",
        service_api_key_present: state.openVikingConfigured,
      };
    }
    return {
      authenticated: true,
      needs_setup: false,
      customer_mode: state.customerMode,
      user: { id: "admin", display_name: "Admin", role: "admin" },
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
vi.mock("@/views/ModelSettingsView", () => ({ default: () => <div>model-body</div> }));
vi.mock("@/views/CandidateReviewView", () => ({ default: () => <div>review-body</div> }));
vi.mock("@/views/HealthView", () => ({
  default: ({
    onSharingConfigChange,
  }: {
    onSharingConfigChange?: (config: {
      enabled: boolean;
      endpoint: string;
      service_api_key_present: boolean;
    }) => void;
  }) => (
    <button
      type="button"
      onClick={() => onSharingConfigChange?.({
        enabled: true,
        endpoint: "http://openviking.test",
        service_api_key_present: true,
      })}
    >
      configure-openviking
    </button>
  ),
}));
vi.mock("@/views/AuditView", () => ({ default: () => <div>audit-body</div> }));
vi.mock("@/views/SessionFilterView", () => ({ default: () => <div>filter-body</div> }));
vi.mock("@/views/LangfuseView", () => ({ default: () => <div>langfuse-body</div> }));
vi.mock("@/views/EvolutionWorkspaceView", () => ({
  default: ({ openVikingConfigured }: { openVikingConfigured: boolean }) => (
    <div>evolution-body-{String(openVikingConfigured)}</div>
  ),
}));
vi.mock("@/views/MiningView", () => ({ default: ({ page }: { page: string }) => <div>mining-{page}</div> }));
vi.mock("@/views/OpenVikingWorkspaceShell", () => ({
  default: ({ mode, labs }: { mode: string; labs?: { skill: ReactNode; memory: ReactNode } }) => (
    <div>workspace-{mode}{labs?.skill}{labs?.memory}</div>
  ),
}));
vi.mock("@/views/SkillLabView", () => ({ default: () => <div>skill-lab-body</div> }));
vi.mock("@/views/MemoryLabView", () => ({ default: () => <div>memory-lab-body</div> }));
vi.mock("@/views/DocsView", () => ({ default: () => <div>docs-body</div> }));
vi.mock("@/views/TenantsView", () => ({ default: () => <div>tenants-body</div> }));
vi.mock("@/views/SkillsView", () => ({ default: () => <div>team-only-replacement</div> }));
import App from "@/App";

beforeEach(() => {
  state.customerMode = true;
  state.openVikingConfigured = true;
  state.tenantId = "default";
  window.history.replaceState(null, "", "/");
});
afterEach(cleanup);

describe("original console functionality", () => {
  it.each([false, true])("keeps every original entry with customer_mode=%s", async (mode) => {
    state.customerMode = mode;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    await within(navigation).findByRole("button", { name: "Agent 工作空间" });
    for (const name of ["挖掘总览", "知识源", "挖掘任务", "运行总览", "数据源接入", "进化链路",
      "Agent 工作空间", "平台资产", "全局模型", "用户与权限", "租户管理", "运行状态", "使用文档"]) {
      expect(within(navigation).getByRole("button", { name })).toBeVisible();
    }
    await screen.findByRole("button", { name: /租户切换/ });
    expect(document.querySelector("aside")?.textContent).toContain("当前租户");
    expect(document.querySelector("main > header")).toBeNull();
  });

  it("renders mining and the full workspace with both labs in customer mode", async () => {
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    fireEvent.click(within(navigation).getByRole("button", { name: "挖掘总览" }));
    expect(screen.getByText("mining-overview")).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: "Agent 工作空间" }));
    expect(screen.getByText("workspace-workspace")).toBeVisible();
    expect(screen.getByText("skill-lab-body")).toBeVisible();
    expect(screen.getByText("memory-lab-body")).toBeVisible();
    expect(screen.queryByText("team-only-replacement")).toBeNull();
    fireEvent.click(within(navigation).getByRole("button", { name: "平台资产" }));
    expect(screen.getByText("workspace-platform")).toBeVisible();
  });

  it("hides OpenViking-backed entries until the service credential is configured", async () => {
    state.openVikingConfigured = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    await waitFor(() => {
      expect(within(navigation).queryByRole("button", { name: "Agent 工作空间" })).not.toBeInTheDocument();
      expect(within(navigation).queryByRole("button", { name: "平台资产" })).not.toBeInTheDocument();
    });
    expect(within(navigation).getByRole("button", { name: "运行状态" })).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: "进化链路" }));
    expect(await screen.findByText("evolution-body-false")).toBeVisible();
  });

  it("redirects a direct OpenViking workspace link while configuration is absent", async () => {
    state.openVikingConfigured = false;
    window.history.replaceState(null, "", "/?view=workspace");
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    await waitFor(() => {
      expect(within(navigation).getByRole("button", { name: "运行总览" })).toHaveAttribute(
        "aria-current",
        "page",
      );
    });
    expect(window.location.search).toContain("view=dashboard");
    expect(screen.queryByText("workspace-workspace")).not.toBeInTheDocument();
  });

  it("reveals OpenViking-backed entries immediately after configuration", async () => {
    state.openVikingConfigured = false;
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).queryByRole("button", { name: "Agent 工作空间" })).not.toBeInTheDocument();

    fireEvent.click(within(navigation).getByRole("button", { name: "运行状态" }));
    fireEvent.click(await screen.findByRole("button", { name: "configure-openviking" }));

    expect(
      await within(navigation).findByRole("button", { name: "Agent 工作空间" }),
    ).toBeVisible();
    expect(within(navigation).getByRole("button", { name: "平台资产" })).toBeVisible();
  });

  it("keeps governance entries when another tenant is selected", async () => {
    state.tenantId = "account-a";
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    for (const name of ["全局模型", "用户与权限", "运行状态"]) {
      expect(within(navigation).getByRole("button", { name })).toBeVisible();
    }
  });
});
