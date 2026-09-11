// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  getTenantConfig: vi.fn(),
  listTenants: vi.fn(),
  updateTenantConfig: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: mocks.api,
  createTenant: vi.fn(),
  getActiveTenantId: () => "tenant-a",
  getTenantConfig: mocks.getTenantConfig,
  listTenants: mocks.listTenants,
  rotateTenantToken: vi.fn(),
  setActiveTenantId: vi.fn(),
  setTenantStatus: vi.fn(),
  updateTenantConfig: mocks.updateTenantConfig,
}));

vi.mock("@/lib/toast", () => ({
  toastErr: vi.fn(),
  toastOk: vi.fn(),
}));

import LangfuseView from "@/views/LangfuseView";
import TenantsView from "@/views/TenantsView";

const admin = { id: "admin", display_name: "Admin", role: "admin" as const };

beforeEach(() => {
  mocks.api.mockReset();
  mocks.getTenantConfig.mockReset();
  mocks.listTenants.mockReset();
  mocks.updateTenantConfig.mockReset();
  mocks.updateTenantConfig.mockResolvedValue({ tenant_id: "tenant-a", config_overrides: {} });
});

afterEach(cleanup);

describe("data-source configuration ownership", () => {
  it("edits the selected tenant conversion mode on the data-source page", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/langfuse/status") {
        return { enabled: false, reachable: false, tracing: { enabled: false } };
      }
      if (path === "/api/tenants/tenant-a/langfuse-config") {
        return {
          enabled: false,
          tracing_enabled: false,
          host: "https://langfuse.example.com",
          public_key_present: true,
          secret_key_present: true,
          mappers: [],
        };
      }
      if (path === "/api/tenants/tenant-a/datasource-config") {
        return {
          source: "langfuse",
          type: "skillopt",
          conversion_mode: "legacy_skillopt",
          legacy_converter_code: "def convert(raw):\n    return {}\n",
        };
      }
      return {};
    });

    render(<LangfuseView active user={admin} />);

    const mode = await screen.findByRole("combobox", { name: "会话转换模式" });
    expect(mode).toHaveValue("skillopt");
    expect(screen.getByText("两种模式都从 Langfuse 拉取 Session，仅转换逻辑不同。")).toBeVisible();

    fireEvent.change(mode, { target: { value: "langfuse" } });
    fireEvent.click(screen.getByRole("button", { name: "保存转换配置" }));

    await waitFor(() => {
      expect(mocks.updateTenantConfig).toHaveBeenCalledWith("tenant-a", {
        datasource_type: "langfuse",
        datasource_legacy_converter_code: "def convert(raw):\n    return {}",
      });
    });
  });

  it("saves outbound tracing through the service-wide endpoint", async () => {
    mocks.api.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/langfuse/status") {
        return { enabled: false, reachable: false, tracing: { enabled: false } };
      }
      if (path === "/api/tenants/tenant-a/langfuse-config") {
        return {
          enabled: false,
          host: "https://tenant-source.example.com",
          public_key_present: true,
          secret_key_present: true,
          mappers: [],
        };
      }
      if (path === "/api/langfuse-tracing-config") {
        if (options?.method === "POST") {
          const body = JSON.parse(String(options.body));
          return {
            ...body,
            public_key_present: true,
            secret_key_present: true,
            status: { enabled: true, initialized: false, host: body.host },
          };
        }
        return {
          enabled: false,
          host: "",
          public_key_present: false,
          secret_key_present: false,
          environment: "local",
          sample_rate: 1,
          capture_content: true,
          status: { enabled: false, initialized: false },
        };
      }
      if (path === "/api/tenants/tenant-a/datasource-config") {
        return { source: "langfuse", type: "langfuse" };
      }
      return {};
    });

    render(<LangfuseView active user={admin} />);

    fireEvent.click(await screen.findByRole("checkbox", {
      name: "上报进化与团队 Memory 链路",
    }));
    fireEvent.change(screen.getByLabelText(/观测 Host/), {
      target: { value: "https://global-observability.example.com" },
    });
    fireEvent.change(screen.getByLabelText(/观测 Public Key/), {
      target: { value: "pk-global" },
    });
    fireEvent.change(screen.getByLabelText(/观测 Secret Key/), {
      target: { value: "sk-global" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存全局观测配置" }));

    await waitFor(() => {
      const call = mocks.api.mock.calls.find(
        ([path, options]) =>
          path === "/api/langfuse-tracing-config"
          && options?.method === "POST"
      );
      expect(call).toBeDefined();
      const payload = JSON.parse(String(call?.[1]?.body));
      expect(payload).toMatchObject({
        enabled: true,
        host: "https://global-observability.example.com",
        public_key: "pk-global",
        secret_key: "sk-global",
        environment: "local",
      });
      expect(payload).not.toHaveProperty("max_sessions");
      expect(payload).not.toHaveProperty("default_trace_name");
    });
    expect(mocks.updateTenantConfig).not.toHaveBeenCalled();
  });

  it("keeps tenant management limited to quotas and a data-source summary", async () => {
    mocks.listTenants.mockResolvedValue({
      mode: "postgres",
      tenants: [{ tenant_id: "tenant-a", display_name: "Acme", status: "active" }],
    });
    mocks.getTenantConfig.mockResolvedValue({
      tenant_id: "tenant-a",
      display_name: "Acme",
      editable_keys: [],
      config_overrides: {
        langfuse_enabled: true,
        langfuse_host: "https://langfuse.example.com",
        datasource_type: "skillopt",
        max_concurrent_sessions: 3,
        max_evolve_per_day: 20,
      },
    });

    render(<TenantsView active user={admin} />);

    fireEvent.click(await screen.findByRole("button", { name: "设置" }));
    expect(await screen.findByRole("button", { name: "打开数据源接入" })).toBeVisible();
    expect(screen.getByText("兼容模式（导入旧版 converter.py）")).toBeVisible();
    expect(screen.queryByText("Public Key")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Converter 源码")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("并发 Session 上限"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("每日 Evolution 上限"), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配额" }));

    await waitFor(() => {
      expect(mocks.updateTenantConfig).toHaveBeenCalledWith("tenant-a", {
        max_concurrent_sessions: 4,
        max_evolve_per_day: 25,
      });
    });
  });
});
