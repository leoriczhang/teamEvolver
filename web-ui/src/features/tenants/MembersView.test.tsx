// @vitest-environment jsdom
import { api } from "@/api/client";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import MembersView from "./MembersView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

const runtime = { tenant_id: "alpha", revision: 3, config: {
  collection_concurrency: 1, ingestion_concurrency: 1, analysis_concurrency: 2,
  evolution_concurrency: 1, replay_concurrency: 1, background_concurrency: 1,
  llm_max_concurrency: 8, llm_queue_capacity: 64,
} };

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api).mockImplementation(async path => {
    if (path === "/api/tenants") return { mode: "postgres", tenants: [] };
    if (path.endsWith("/members")) return { members: [] };
    if (path.endsWith("/config")) return { config_overrides: {} };
    if (path.endsWith("/runtime-config")) return runtime;
    return { agent_token: "tevt_retained", available: true };
  });
});
afterEach(cleanup);

it("displays the same current Key again after reopening without rotation", async () => {
  const first = render(<MembersView active tenantId="alpha" />);
  await screen.findByText("tevt_retained");
  first.unmount();
  render(<MembersView active tenantId="alpha" />);
  await screen.findByText("tevt_retained");
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("rotate-token"))).toBe(false);
});

it("imports the original legacy Key without rotation", async () => {
  vi.mocked(api).mockImplementation(async (path, request) => {
    if (path === "/api/tenants") return { mode: "postgres", tenants: [] };
    if (path.endsWith("/members")) return { members: [] };
    if (path.endsWith("/config")) return { config_overrides: {} };
    if (path.endsWith("/runtime-config")) return runtime;
    return request?.method === "PUT"
      ? { agent_token: "tevt_original", available: true } : { agent_token: null, available: false };
  });
  render(<MembersView active tenantId="alpha" />);
  fireEvent.change(await screen.findByLabelText("原租户 Key"), { target: { value: "tevt_original" } });
  fireEvent.click(screen.getByRole("button", { name: "保存原 Key" }));
  await screen.findByText("tevt_original");
  const imported = vi.mocked(api).mock.calls.find(([, request]) => request?.method === "PUT")!;
  expect(JSON.parse(imported[1]?.body as string)).toEqual({ agent_token: "tevt_original" });
  await waitFor(() => expect(screen.queryByLabelText("原租户 Key")).toBeNull());
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("rotate-token"))).toBe(false);
});

it("lets tenant administrators save their own execution capacity", async () => {
  render(<MembersView active tenantId="alpha" />);
  fireEvent.change(await screen.findByLabelText(/Session 分析/), { target: { value: "4" } });
  fireEvent.click(screen.getByRole("button", { name: "保存执行容量" }));
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, request]) =>
    path === "/api/tenants/alpha/runtime-config" && request?.method === "PUT")).toBe(true));
  const write = vi.mocked(api).mock.calls.find(([path, request]) =>
    path === "/api/tenants/alpha/runtime-config" && request?.method === "PUT")!;
  expect(JSON.parse(String(write[1]?.body))).toMatchObject({
    expected_revision: 3, config: { analysis_concurrency: 4 },
  });
});
