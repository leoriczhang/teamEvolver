// @vitest-environment jsdom
import { webcrypto } from "node:crypto";
import { api } from "@/api/client";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ConnectionForm from "./ConnectionForm";
import { saveConnection, startMAJob, type MAConnection } from "./api/client";

vi.mock("@/api/client", () => ({
  api: vi.fn(), tenantHeaders: () => new Headers(), getActiveTenantId: () => "fixture-tenant",
}));

const job = { job_id: "fixture-job", status_url: "/api/jobs/fixture-job" };
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("crypto", { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) });
  vi.mocked(api).mockImplementation(async path => path.endsWith("/token")
    ? { available: true, tenant_id: "fixture-tenant", agent_token: "tevt_fixture-retained" } : job);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each([undefined, "existing"])("saves an MA connection without randomUUID: %s", async id => {
  const body = { display_name: "A2V fixture", expected_revision: id ? 2 : 0 };
  await expect(Promise.resolve().then(() => saveConnection(body, id))).resolves.toEqual(job);
  const [path, request] = vi.mocked(api).mock.calls[0];
  expect(path).toBe(id ? "/api/ma/connections/existing" : "/api/ma/connections");
  expect(request?.method).toBe(id ? "PUT" : "POST");
  expect(JSON.parse(request?.body as string)).toEqual(body);
  expect(new Headers(request?.headers).get("Idempotency-Key")).toMatch(uuidPattern);
});

it.each(["discover", "collect", "reconcile"] as const)("starts %s without randomUUID", async kind => {
  await expect(startMAJob("existing", kind)).resolves.toEqual(job);
  const [path, request] = vi.mocked(api).mock.calls[0];
  expect(path).toBe(`/api/ma/connections/existing/${kind}`);
  expect(new Headers(request?.headers).get("Idempotency-Key")).toMatch(uuidPattern);
});

it("submits the new MA form without the screenshot UUID error", async () => {
  const saved = vi.fn();
  render(<ConnectionForm onClose={vi.fn()} onSaved={saved} />);
  for (const [label, value] of [
    ["连接名称", "A2V fixture"], ["入口 Agent ID", "agent-fixture"],
    ["业务 Environment ID", "env-fixture"], ["MA Key", "fixture-ma-key"],
    ["当前租户 Key（tevt_）", "fixture-tenant-key"],
  ]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  fireEvent.submit(screen.getByRole("button", { name: "保存并发现资源" }).closest("form")!);
  await waitFor(() => expect(saved).toHaveBeenCalledWith(job));
  expect(screen.queryByRole("alert")).toBeNull();
  const save = vi.mocked(api).mock.calls.find(([path]) => path === "/api/ma/connections")!;
  expect(new Headers(save[1]?.headers).get("Idempotency-Key")).toMatch(uuidPattern);
  expect((screen.getByLabelText("MA Key") as HTMLInputElement).value).toBe("");
});

it("automatically reuses the saved tenant Key without requesting rotation", async () => {
  const saved = vi.fn();
  render(<ConnectionForm onClose={vi.fn()} onSaved={saved} />);
  await waitFor(() => expect(screen.getByLabelText("当前租户 Key（tevt_）")).toHaveValue("tevt_fixture-retained"));
  expect(screen.getByLabelText("当前租户 Key（tevt_）")).toHaveAttribute("type", "text");
  for (const [label, value] of [
    ["连接名称", "A2V fixture"], ["入口 Agent ID", "agent-fixture"],
    ["业务 Environment ID", "env-fixture"], ["MA Key", "fixture-ma-key"],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.submit(screen.getByRole("button", { name: "保存并发现资源" }).closest("form")!);
  await waitFor(() => expect(saved).toHaveBeenCalledWith(job));
  const save = vi.mocked(api).mock.calls.find(([path]) => path === "/api/ma/connections")!;
  expect(JSON.parse(save[1]?.body as string).tenant_token).toBe("tevt_fixture-retained");
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("rotate"))).toBe(false);
});

it("keeps the credential on an existing connection when only editing settings", async () => {
  const saved = vi.fn();
  render(<ConnectionForm connection={{
    connection_id: "existing", revision: 2,
    config: { display_name: "Existing", entry_agent_id: "agent", environment_id: "env" },
  } as MAConnection} onClose={vi.fn()} onSaved={saved} />);
  expect(screen.getByLabelText("租户 Key（留空保留）")).toHaveValue("");
  fireEvent.submit(screen.getByRole("button", { name: "保存配置" }).closest("form")!);
  await waitFor(() => expect(saved).toHaveBeenCalledWith(job));
  expect(api).toHaveBeenCalledOnce();
  expect(JSON.parse(vi.mocked(api).mock.calls[0][1]?.body as string).tenant_token).toBe("");
});

it("allows connecting by Agent ID without manually supplying an environment", async () => {
  const saved = vi.fn();
  render(<ConnectionForm onClose={vi.fn()} onSaved={saved} />);
  await waitFor(() => expect(screen.getByLabelText("当前租户 Key（tevt_）")).toHaveValue("tevt_fixture-retained"));
  expect(screen.getByLabelText("业务 Environment ID")).not.toBeRequired();
  for (const [label, value] of [
    ["连接名称", "Automatic"], ["入口 Agent ID", "agent-fixture"], ["MA Key", "fixture-ma-key"],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.submit(screen.getByRole("button", { name: "保存并发现资源" }).closest("form")!);
  await waitFor(() => expect(saved).toHaveBeenCalledWith(job));
  const save = vi.mocked(api).mock.calls.find(([path]) => path === "/api/ma/connections")!;
  expect(JSON.parse(save[1]?.body as string).environment_id).toBe("");
});
