// @vitest-environment jsdom
import DataSourcesView from "@/features/sessions/DataSourcesView";
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api, tenantHeaders: () => new Headers() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

const admin = {
  id: "admin",
  display_name: "Admin",
  role: "admin" as const,
  platform_role: "platform_admin" as const,
};
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
  source_editable: true,
  tenant_id: "a", file: "a.py", configured: true, enabled: true, provider: "doris",
  persistence: { durable: true, mode: "postgres" },
  available: [{ file: "a.py" }, { file: "b.py", bound_tenant_id: "b" }],
  supported_filters: ["from_timestamp", "to_timestamp"],
  required_filters: ["from_timestamp", "to_timestamp"],
  schedule: {
    enabled: false,
    time: "00:00",
    timezone: "Asia/Shanghai",
    window: "previous_day",
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
    if (path.startsWith("/api/datasource/code?")) return { file: "a.py", code, revision: 1 };
    if (path === "/api/datasource/code" && options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      return { file: body.file, revision: 2 };
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
    if (path === "/api/datasource/test") return { job_id: "health", status_url: "/api/jobs/health" };
    if (path === "/api/datasource/sessions") return { job_id: "preview", status_url: "/api/jobs/preview" };
    if (path.startsWith("/api/jobs/")) return { status: "succeeded" };
    if (path.includes("/jobs/health/result")) return { result: { health: { ok: true } } };
    if (path.includes("/jobs/preview/result")) return { result: { sessions: [{ session_id: "s1" }] } };
    if (path === "/api/datasource/pull") {
      return {
        accepted: true, job_id: "manual-1", status_url: "/api/jobs/manual-1",
        pull_status: {
          running: false,
          job_id: "manual-1",
          job_type: "manual",
          last_status: "succeeded",
          last_total: 1,
          last_counts: { queued: 1 },
          last_results: [{ session_id: "s1", status: "queued" }],
          last_results_truncated: false,
        },
      };
    }
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
        accepted: true, job_id: "scheduled-1", status_url: "/api/jobs/scheduled-1",
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

it("renders persistent versions and tenant-owned bindings", async () => {
  render(<DataSourcesView active user={admin} />);
  await waitFor(() => expect(screen.getByLabelText("适配器文件")).toHaveValue("a.py"));
  expect(screen.getByText("适配器版本已持久化")).toBeVisible();
  expect(screen.getByText(/所有实例读取同一份适配器/)).toBeVisible();
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
  fireEvent.click(screen.getByRole("button", { name: "保存新版本" }));
  await waitFor(() => {
    const call = mocks.api.mock.calls.find(([path, options]) => path === "/api/datasource/code" && options?.method === "PUT");
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ file: "a.py", expected_revision: 1 });
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
  fireEvent.click(screen.getByRole("button", { name: "保存新版本" }));
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

it("pulls the complete filtered window without a session limit", async () => {
  render(<DataSourcesView active user={admin} />);
  await screen.findByLabelText("适配器文件");
  fireEvent.change(screen.getByLabelText("起始时间"), {
    target: { value: "2026-09-01T00:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间"), {
    target: { value: "2026-09-02T00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "拉取入库" }));

  await waitFor(() => {
    const call = mocks.api.mock.calls.find(
      ([path]) => path === "/api/datasource/pull",
    );
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.max_sessions).toBeUndefined();
  });
  expect(await screen.findByText(/正在拉取/)).toBeVisible();
});

it("does not request adapter data for non-admins", () => {
  render(<DataSourcesView active user={{ ...admin, role: "user" }} />);
  expect(mocks.api).not.toHaveBeenCalled();
});
