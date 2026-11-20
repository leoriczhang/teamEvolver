// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import DataSourcesView from "@/features/sessions/DataSourcesView";
import ExecutionProfilePanel from "@/features/skills/ExecutionProfilePanel";

vi.mock("@/api/client", () => ({ api: vi.fn(), tenantHeaders: () => new Headers() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const endpoint = "/api/datasource/connections";
const schedule = { enabled: false, time: "02:00", timezone: "Asia/Shanghai", window: "previous_day" };

it("edits a pinned named connection through the datasource page and watches its own pull", async () => {
  let connection = {
    connection_id: "business-a", label: "业务 A", revision: 3,
    adapter_id: "shared.py", adapter_revision: 5, enabled: true,
    settings: { profile: "default", options: {} }, default_filters: { agent_id: "agent-a" }, schedule,
  };
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === "/api/datasource") return {
      tenant_id: "alpha", source_editable: false, configured: false, enabled: false, file: "",
      available: [{ file: "shared.py", revision: 7 }], schedule,
      schedule_status: { running: false, job_id: "someone-elses-pull", last_status: "succeeded" },
    };
    if (path === endpoint) return { connections: [connection] };
    if (path === `${endpoint}/business-a` && options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      connection = { ...connection, ...body, revision: 4 };
      return connection;
    }
    if (path === `${endpoint}/business-a/pull`) return { job_id: "named-run:session.pull" };
    if (path === "/api/datasource/jobs/named-run%3Asession.pull/progress?limit=1") return {
      status: "running", complete: false, tracking_available: true,
      counts: { fetched: 1, enqueued: 0, duplicate: 1, error: 0 },
      summary: { leaf: { archived: 0, skipped: 0, pending: 2, failed: 0, cancelled: 0 } },
    };
    throw new Error(`Unexpected API request: ${path}`);
  });
  render(<DataSourcesView active user={{ id: "admin", role: "admin", platform_role: "user" }} />);
  fireEvent.click(await screen.findByText("独立数据源连接"));
  const selector = await screen.findByLabelText("选择独立连接");
  await screen.findByRole("option", { name: "业务 A · v3" });
  fireEvent.change(selector, { target: { value: "business-a" } });
  expect(screen.getByLabelText("固定文件版本")).toHaveValue(5); // The newer file head must not rebind it.
  fireEvent.change(screen.getByLabelText("起始时间（本地）"), { target: { value: "2026-11-19T00:00" } });
  fireEvent.change(screen.getByLabelText("结束时间（本地）"), { target: { value: "2026-11-20T00:00" } });
  fireEvent.change(screen.getByLabelText("默认过滤条件 JSON"), {
    target: { value: JSON.stringify({ agent_id: "corrected-agent-a" }) },
  });
  expect(screen.getByRole("button", { name: "拉取此连接" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存独立连接" }));
  await screen.findByRole("option", { name: "业务 A · v4" });
  const saved = vi.mocked(api).mock.calls.find(([path, options]) => path === `${endpoint}/business-a` && options?.method === "PUT");
  expect(JSON.parse(String(saved?.[1]?.body))).toMatchObject({
    expected_revision: 3, adapter_revision: 5,
    settings: { profile: "default", options: {} }, default_filters: { agent_id: "corrected-agent-a" },
  });
  fireEvent.click(screen.getByRole("button", { name: "拉取此连接" }));
  await screen.findByText("named-run:session.pull");
  expect(await screen.findByText(/已归档 0 · 已跳过 0 · 处理中 2/)).toBeVisible();
  expect(screen.getByText(/新入队 0 · 重复 1/)).toBeVisible();
  expect(vi.mocked(api).mock.calls.some(([path]) => path === "/api/datasource/pull")).toBe(false);
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("someone-elses-pull/progress"))).toBe(false);
});

it("uploads a preset-derived registration JSON, previews it, then submits the exact frozen draft", async () => {
  const endpoint = "/api/skill-evolution/execution-profiles";
  const original = {
    profile_id: "customer-preset-v2", runtime_type: "agentscope-java", agent_id: "customer-agent",
    user_id: "customer-execution", affected_skill_ids: [],
    components: [{ component_id: "customer-preset-v2:agent-prompt", kind: "agent_prompt", slot: "system", order: 0,
      content: "按实际资料回答。{{ rule }}" }],
    environment: {
      request: { model: "observed-business-model", temperature: 0.3, tools: [] },
      template_variables: { rule: "缺失事实保持未知。" },
      template_input_contract: { schema_version: "teamevolver.template-input-contract.v1", variable_names: ["rule"] },
      message_prefixes: { user: "HWP：", assistant: "C：" },
    },
  };
  const selected = vi.fn();
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === endpoint && !options?.method) return { profiles: [] };
    if (path === `${endpoint}/preview`) return {
      profile_id: original.profile_id, preview_hash: "server-preview",
      summary: { component_count: 1, affected_skill_count: 0 },
    };
    if (path === endpoint && options?.method === "POST") return {
      ...original, current_release_id: "registered-baseline", revision: 1,
    };
    throw new Error(`Unexpected API request: ${path}`);
  });
  render(<ExecutionProfilePanel active isAdmin profileId="" onChange={selected} />);
  fireEvent.click(screen.getByText("登记现有业务 Agent 执行基线"));
  const file = new File([JSON.stringify(original)], "verified-preset-profile.json", { type: "application/json" });
  // jsdom lacks File.text(); browsers supply this standard file-reading API.
  Object.defineProperty(file, "text", { value: async () => JSON.stringify(original) });
  fireEvent.change(screen.getByLabelText("从登记 JSON 预览"), { target: { files: [file] } });
  await screen.findByRole("region", { name: "业务基线登记预览" });
  expect(screen.getByText(/customer-preset-v2 · 1 个组件 · 0 个 Skill 依赖/)).toBeVisible();
  expect(vi.mocked(api).mock.calls.filter(([path, options]) => path === endpoint && options?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "登记业务执行基线" }));
  await waitFor(() => expect(selected).toHaveBeenCalledWith(original.profile_id));
  const preview = vi.mocked(api).mock.calls.find(([path]) => path === `${endpoint}/preview`);
  const submitted = vi.mocked(api).mock.calls.find(([path, options]) => path === endpoint && options?.method === "POST");
  expect(JSON.parse(String(preview?.[1]?.body))).toEqual(original);
  expect(JSON.parse(String(submitted?.[1]?.body))).toEqual(original);
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("/evolve") || path.includes("/settings"))).toBe(false);
});
