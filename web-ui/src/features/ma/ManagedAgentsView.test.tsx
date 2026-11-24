// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ManagedAgentsView from "./ManagedAgentsView";
import ConnectionForm from "./ConnectionForm";
import CandidateTargets from "./CandidateTargets";
import { followMAJob } from "./api/client";
import ResourceBindings from "./ResourceBindings";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({
  api: mocks.api, tenantHeaders: () => new Headers(), getActiveTenantId: () => "",
}));
const connection = {
  connection_id: "generic", revision: 2, enabled: true, status: "drift",
  config: { display_name: "笔记 MA", entry_agent_id: "root", environment_id: "env", default_user_id: "employee",
    ma_credential_configured: true, tenant_credential_configured: true, vault_ids: [], replay_vault_ids: [], runtime_binding: {} },
  detail: { managed_skill_count: 1, error: "drift: managed Skill changed externally" },
  sessions: [{ session_id: "business-session", record: {
    business_time: "2026-10-20T12:00:00Z", last_pulled_at: "2026-10-20T13:00:00Z", user_id: "employee" } }],
};
const resources = [{ kind: "agent", logical_name: "root", revision: 1, record: { name: "编辑", version: "2" } }];
describe("MA console", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.api.mockImplementation(async (path: string) => {
      if (path.includes("/resources?")) return { resources: path.includes("kind=agent") ? resources : [] };
      if (path.endsWith("/releases")) return { releases: [] };
      if (path.endsWith("/generic")) return connection;
      return { connections: [connection] };
    });
  });
  it("shows drift, masked credentials and both business and pull times", async () => {
    const view = render(<ManagedAgentsView active canManage />);
    await screen.findByText("版本漂移");
    expect(await screen.findByText("drift: managed Skill changed externally")).toBeTruthy();
    expect(screen.getByText(/MA 凭证已配置/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看租户 Key" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "会话采集" }));
    expect(screen.getByText("业务发生时间")).toBeTruthy();
    expect(screen.getByText("拉取时间")).toBeTruthy();
    expect(screen.getByText("business-session")).toBeTruthy();
    view.unmount();
  });
  it("keeps member access read only", async () => {
    const view = render(<ManagedAgentsView active />);
    await screen.findByText("版本漂移");
    expect(screen.queryByText("新建连接")).toBeNull();
    expect(screen.queryByText("暂停")).toBeNull();
    expect(screen.queryByText("补采会话")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看租户 Key" })).toBeNull();
    view.unmount();
  });
  it("distinguishes configuration readiness from execution and discloses custom tools", async () => {
    const value = { ...connection, detail: { replay_capability: {
      supported: true, assessment: "configuration_only", custom_tool_names: ["render_video"],
    } } };
    mocks.api.mockImplementation(async (path: string) => {
      if (path.includes("/resources?")) return { resources: [] };
      if (path.endsWith("/releases")) return { releases: [] };
      if (path.endsWith("/generic")) return value;
      return { connections: [value] };
    });
    const view = render(<ManagedAgentsView active />);
    expect(await screen.findByText("配置检查通过")).toBeTruthy();
    expect(screen.getByText(/配置检查不代表回放验证通过/)).toBeTruthy();
    expect(screen.getByText(/自定义工具执行器尚未接入：render_video/)).toBeTruthy();
    expect(screen.queryByText("可执行")).toBeNull();
    view.unmount();
  });
  it("enables replay without manual resource IDs and follows resource preparation", async () => {
    const job = { job_id: "prepare", status_url: "/api/jobs/prepare" };
    mocks.api.mockResolvedValue(job);
    const saved = vi.fn();
    const disabled = { ...connection, config: { ...connection.config, replay_enabled: false } };
    const view = render(<ConnectionForm connection={disabled as any} onClose={() => {}} onSaved={saved} />);
    expect((screen.getByLabelText("MA Key（留空保留）") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("租户 Key（留空保留）") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByLabelText("启用隔离 MA True Replay"));
    expect(screen.getByText("保存后自动读取来源配置")).toBeTruthy();
    expect(screen.queryByLabelText("回放 Environment ID")).toBeNull();
    expect(screen.queryByLabelText("回放 Vault ID（逗号分隔）")).toBeNull();
    fireEvent.submit(screen.getByText("保存配置").closest("form")!);
    await waitFor(() => expect(saved).toHaveBeenCalledWith(job));
    const body = JSON.parse(mocks.api.mock.calls[0][1].body);
    expect(body.replay_enabled).toBe(true);
    expect(body.replay_environment_id).toBe("");
    expect(body.replay_vault_ids).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
    view.unmount();
  });
  it("shows source references read only and explains native Session isolation", () => {
    const value = { ...connection, config: { ...connection.config, replay_enabled: true },
      detail: { replay_resources: { status: "ready", environment_id: "env-source",
        vault_ids: ["vlt-source"] } } };
    const view = render(<ConnectionForm connection={value as any} onClose={() => {}} onSaved={() => {}} />);
    expect(screen.getByText("env-source")).toBeTruthy();
    expect(screen.getByText("vlt-source")).toBeTruthy();
    expect(screen.getByText(/两个独立 Session 中执行相同任务，仅替换目标 Skill/)).toBeTruthy();
    expect(screen.queryByDisplayValue("env-source")).toBeNull();
    view.unmount();
  });
  it("allows clearing an unverified default owner", async () => {
    mocks.api.mockResolvedValue(connection);
    const view = render(<ConnectionForm connection={connection as any} onClose={() => {}} onSaved={() => {}} />);
    const field = screen.getByLabelText("默认归属用户 ID（可选）") as HTMLInputElement;
    expect(field.required).toBe(false);
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.submit(screen.getByText("保存配置").closest("form")!);
    await waitFor(() => expect(mocks.api).toHaveBeenCalled());
    const request = JSON.parse(mocks.api.mock.calls[0][1].body);
    expect(request.default_user_id).toBe("");
    expect(request.expected_revision).toBe(2);
    view.unmount();
  });
  it("lets a new candidate choose target members and shows target verdict", async () => {
    const view = render(<CandidateTargets skillName="new-note" canManage
      matrix={{ generic: { status: "missing", accepted: false } }} />);
    await screen.findByText("笔记 MA");
    expect(screen.getByText("缺少独立验证")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("编辑"));
    fireEvent.click(screen.getByText("保存目标关联"));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(
      "/api/ma/connections/generic/resources/new-note/bindings",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ expected_revision: 0, agent_ids: ["root"] }) }),
    ));
    view.unmount();
  });
  it("follows accepted jobs and surfaces failure without resubmitting", async () => {
    mocks.api.mockResolvedValue({ status: "failed", error_code: "ma_validation_failed" });
    const status = vi.fn();
    await expect(followMAJob({ job_id: "job", status_url: "/api/jobs/job" }, new AbortController().signal, status))
      .rejects.toThrow("ma_validation_failed");
    expect(status).toHaveBeenCalledWith("failed");
    expect(mocks.api).toHaveBeenCalledTimes(1);
  });
  it.each(["succeeded", "failed"])("waits for the native binding job: %s", async status => {
    let finish!: (value: unknown) => void;
    mocks.api.mockImplementation(async (path: string) => {
      if (path.endsWith("/bindings")) return { job_id: "binding", status_url: "/api/jobs/binding" };
      if (path === "/api/jobs/binding") return new Promise(resolve => { finish = resolve; });
      throw new Error(path);
    });
    const saved = vi.fn();
    const view = render(<ResourceBindings connectionId="generic" canManage onSaved={saved}
      resources={[...resources, { kind: "skill", logical_name: "note", revision: 2,
        record: { id: "skill-note", latest_version: "1", agents: [], bound_versions: {} } }] as any} />);
    expect(screen.getByText("已发布，未挂载")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑关联" }));
    fireEvent.click(screen.getByLabelText("编辑"));
    fireEvent.click(screen.getByRole("button", { name: "保存关联" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/api/jobs/binding", expect.anything()));
    expect(screen.getByRole("button", { name: "正在应用关联…" }).hasAttribute("disabled")).toBe(true);
    expect(saved).not.toHaveBeenCalled();
    finish({ status, error_code: status === "failed" ? "ma_validation_failed" : "" });
    if (status === "succeeded") await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
    else {
      expect((await screen.findByRole("alert")).textContent).toContain("ma_validation_failed");
      expect(saved).not.toHaveBeenCalled();
      expect(screen.getByText("已发布，未挂载")).toBeTruthy();
    }
    view.unmount();
  });
});
