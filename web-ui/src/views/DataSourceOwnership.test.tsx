// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
import DataSourcesView from "./DataSourcesView";

const admin = { id: "admin", display_name: "Admin", role: "admin" as const };
const code = `SOURCE = {"label": "A", "provider": "test", "supported_filters": ["from_timestamp", "to_timestamp"]}
def build_adapter():
    return Adapter()
class Adapter:
    def health(self): return {"ok": True}
    def list_session_ids(self, filters, *, max_sessions): return ["s1"]
    def fetch_session(self, session_id): return {"id": session_id}, []
    def convert_session(self, raw, traces): return {"session_id": raw["id"], "turns": []}
    def close(self): pass
`;
const source = {
  tenant_id: "a", file: "a.py", configured: true, enabled: true, provider: "doris",
  persistence: { durable: false, mode: "runtime_only", warning: "运行副本可能丢失，请联系项目 Owner 合入源码。" },
  available: [{ file: "a.py" }, { file: "b.py", bound_tenant_id: "b" }],
  supported_filters: ["from_timestamp", "to_timestamp"],
  required_filters: ["from_timestamp", "to_timestamp"],
  schedule: {
    enabled: false,
    time: "00:00",
    timezone: "Asia/Shanghai",
    window: "previous_day",
    max_sessions: 1000,
  },
  schedule_status: {
    running: false,
    next_run_at: "",
    last_status: "",
  },
};

beforeEach(() => {
  mocks.api.mockReset();
  mocks.api.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === "/api/datasource") return source;
    if (path.startsWith("/api/datasource/code?")) return { file: "a.py", code, revision: "rev-1" };
    if (path === "/api/datasource/code" && options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      return { file: body.file, revision: "rev-2" };
    }
    if (path === "/api/datasource/code/test") {
      const body = JSON.parse(String(options?.body));
      return {
        ok: true, source: "editor",
        metadata: { file: body.file, supported_filters: ["from_timestamp", "to_timestamp"], required_filters: ["from_timestamp", "to_timestamp"] },
        health: body.mode === "health" ? { ok: true } : undefined,
        sessions: body.mode === "preview" ? [{ session_id: "draft-1" }] : undefined,
        count: body.mode === "preview" ? 1 : undefined,
      };
    }
    if (path === "/api/datasource/test") return { ok: true };
    if (path === "/api/datasource/sessions") return { sessions: [{ session_id: "s1" }] };
    if (path === "/api/datasource/schedule" && options?.method === "PUT") {
      return {
        schedule: JSON.parse(String(options.body)),
        schedule_status: { running: false, next_run_at: "2026-09-19T16:00:00Z" },
      };
    }
    if (path === "/api/datasource/schedule") {
      return {
        schedule: source.schedule,
        schedule_status: source.schedule_status,
      };
    }
    if (path === "/api/datasource/schedule/run") {
      return {
        accepted: true,
        schedule_status: {
          running: true,
          last_status: "running",
          last_target_date: "2026-09-17",
        },
      };
    }
    return {};
  });
});
afterEach(cleanup);

it("renders one tenant file and a non-persistence warning", async () => {
  render(<DataSourcesView active user={admin} />);
  await waitFor(() => expect(screen.getByLabelText("适配器文件")).toHaveValue("a.py"));
  expect(screen.getByText("适配器修改暂不做持久化托管")).toBeVisible();
  expect(screen.getByText(/联系项目 Owner/)).toBeVisible();
  expect(screen.getByRole("option", { name: "b.py（已绑定其他租户）" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "预览已保存" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("起始时间"), { target: { value: "2026-09-01T00:00" } });
  fireEvent.change(screen.getByLabelText("结束时间"), { target: { value: "2026-09-02T00:00" } });
  fireEvent.click(screen.getByRole("button", { name: "预览已保存" }));
  expect(await screen.findByText("s1")).toBeVisible();
});

it("edits, validates and saves with its opened revision", async () => {
  render(<DataSourcesView active user={admin} />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑源码" }));
  const editor = await screen.findByLabelText("适配器源码");
  fireEvent.change(editor, { target: { value: `${code}\n# edited` } });
  fireEvent.click(screen.getByRole("button", { name: "校验草稿" }));
  expect(await screen.findByLabelText("草稿测试结果")).toHaveTextContent('"source": "editor"');
  fireEvent.click(screen.getByRole("button", { name: "保存运行副本" }));
  await waitFor(() => {
    const call = mocks.api.mock.calls.find(([path, options]) => path === "/api/datasource/code" && options?.method === "PUT");
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ file: "a.py", expected_revision: "rev-1" });
  });
});

it("loads an uploaded py file as a draft before writing it", async () => {
  render(<DataSourcesView active user={admin} />);
  await screen.findByLabelText("适配器文件");
  const uploaded = new File([code], "uploaded.py", { type: "text/x-python" });
  fireEvent.change(screen.getByLabelText("上传适配器文件"), {
    target: { files: [uploaded] },
  });
  expect(await screen.findByLabelText("适配器文件名")).toHaveValue("uploaded.py");
  expect(screen.getByLabelText("适配器源码")).toHaveValue(code);
  expect(
    mocks.api.mock.calls.filter(([path]) => path === "/api/datasource/code"),
  ).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "保存运行副本" }));
  await waitFor(() => {
    const call = mocks.api.mock.calls.find(
      ([path, options]) => path === "/api/datasource/code" && options?.method === "PUT",
    );
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      file: "uploaded.py",
      expected_revision: null,
    });
  });
});

it("tests the saved connection and can explicitly unbind it", async () => {
  render(<DataSourcesView active user={admin} />);
  await waitFor(() => expect(
    screen.getByRole("button", { name: "测试已保存连接" }),
  ).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "测试已保存连接" }));
  expect(await screen.findByRole("status")).toHaveTextContent("连接正常");
  fireEvent.change(screen.getByLabelText("适配器文件"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "保存绑定" }));
  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(
    "/api/datasource",
    expect.objectContaining({ method: "PUT", body: JSON.stringify({ file: "" }) }),
  ));
});

it("saves the daily pull schedule", async () => {
  render(<DataSourcesView active user={admin} />);
  const enabled = await screen.findByLabelText("启用每日定时拉取");
  fireEvent.click(enabled);
  fireEvent.change(screen.getByLabelText("每日触发时间"), {
    target: { value: "01:30" },
  });
  fireEvent.change(screen.getByLabelText("定时拉取最大 Session 数"), {
    target: { value: "250" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存定时配置" }));

  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(
    "/api/datasource/schedule",
    expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({
        enabled: true,
        time: "01:30",
        timezone: "Asia/Shanghai",
        window: "previous_day",
        max_sessions: 250,
      }),
    }),
  ));
});

it("starts an enabled scheduled pull asynchronously", async () => {
  render(<DataSourcesView active user={admin} />);
  fireEvent.click(await screen.findByLabelText("启用每日定时拉取"));
  fireEvent.click(screen.getByRole("button", { name: "保存定时配置" }));
  await waitFor(() => expect(
    screen.getByRole("button", { name: "立即拉取昨日 Trace" }),
  ).toBeEnabled());

  fireEvent.click(screen.getByRole("button", { name: "立即拉取昨日 Trace" }));

  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(
    "/api/datasource/schedule/run",
    { method: "POST" },
  ));
  expect(await screen.findByText("拉取中")).toBeVisible();
});

it("does not request adapter data for non-admins", () => {
  render(<DataSourcesView active user={{ ...admin, role: "user" }} />);
  expect(mocks.api).not.toHaveBeenCalled();
});
