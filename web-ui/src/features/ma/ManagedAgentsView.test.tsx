// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ManagedAgentsView from "./ManagedAgentsView";
import ConnectionForm from "./ConnectionForm";
import CandidateTargets from "./CandidateTargets";
import { followMAJob } from "./api/client";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api, tenantHeaders: () => new Headers() }));
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
    view.unmount();
  });
  it("enables replay without manual resource IDs and follows resource preparation", async () => {
    const job = { job_id: "prepare", status_url: "/api/jobs/prepare" };
    mocks.api.mockResolvedValue(job);
    const saved = vi.fn();
    const view = render(<ConnectionForm connection={connection as any} onClose={() => {}} onSaved={saved} />);
    expect((screen.getByLabelText("MA Key（留空保留）") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("租户 Key（留空保留）") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByLabelText("启用隔离 MA True Replay"));
    expect(screen.getByText("保存后自动准备隔离资源")).toBeTruthy();
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
  it("shows automatic IDs read only and explains missing credentials", () => {
    const value = { ...connection, config: { ...connection.config, replay_enabled: true },
      detail: { replay_resources: { status: "credentials_missing", environment_id: "env-auto",
        vault_ids: ["vlt-auto"], missing_credentials: ["OPENVIKING_API_KEY"] } } };
    const view = render(<ConnectionForm connection={value as any} onClose={() => {}} onSaved={() => {}} />);
    expect(screen.getByText("env-auto")).toBeTruthy();
    expect(screen.getByText("vlt-auto")).toBeTruthy();
    expect(screen.getByText(/需要向回放 Vault 补充：OPENVIKING_API_KEY/)).toBeTruthy();
    expect(screen.queryByDisplayValue("env-auto")).toBeNull();
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
});
