// @vitest-environment jsdom
import { api } from "@/api/client";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import TenantsView from "./TenantsView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastOk: vi.fn(), toastErr: vi.fn() }));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === "/api/tenants") return {
      mode: "postgres", tenants: [{ tenant_id: "alpha", display_name: "Alpha", status: "active" }],
    };
    if (String(path).endsWith("/runtime-config")) {
      const config = {
        collection_concurrency: 1, ingestion_concurrency: 1, analysis_concurrency: 2,
        evolution_concurrency: 1, replay_concurrency: 1, background_concurrency: 1,
        llm_max_concurrency: 8, llm_queue_capacity: 64,
      };
      return { tenant_id: "alpha", revision: 7, config: options?.body ? JSON.parse(String(options.body)).config : config };
    }
    return { tenant_id: "alpha", config_overrides: {} };
  });
});

it("loads independent capacity and saves it against the loaded revision", async () => {
  render(<TenantsView active user={{ tenant_id: "alpha", role: "admin", platform_role: "platform_admin" } as any} />);
  fireEvent.click(await screen.findByRole("button", { name: "设置" }));
  const analysis = await screen.findByLabelText(/Session 分析/);
  expect(analysis).toHaveValue(2);
  fireEvent.change(analysis, { target: { value: "4" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/tenants/alpha/runtime-config", expect.objectContaining({
    method: "PUT",
  })));
  const write = vi.mocked(api).mock.calls.find(([path, request]) =>
    path === "/api/tenants/alpha/runtime-config" && request?.method === "PUT");
  expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
    expected_revision: 7, config: { analysis_concurrency: 4 },
  });
});

it("rejects a model queue smaller than model concurrency before writing", async () => {
  render(<TenantsView active user={{ tenant_id: "alpha", role: "admin", platform_role: "platform_admin" } as any} />);
  fireEvent.click(await screen.findByRole("button", { name: "设置" }));
  fireEvent.change(await screen.findByLabelText(/模型队列容量/), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  expect(vi.mocked(api).mock.calls.some(([, request]) => request?.method === "PUT")).toBe(false);
});
