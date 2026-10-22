// @vitest-environment jsdom
import { api } from "@/api/client";
import HealthView from "@/features/operations/HealthView";
import type { SharingConfig, VikingDirsReport } from "@/features/operations/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { toastErr, toastOk } from "@/lib/toast";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/features/skills/api/skillJobs", () => ({ runSkillCommand: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

const mockedApi = vi.mocked(api);
const mockedCommand = vi.mocked(runSkillCommand);
const endpoint = "http://ov.example.test:1933";
let sharing: SharingConfig;
let storageFails: boolean;
let failStorageAfterSave: boolean;

function endpointStatus() {
  return screen.getByLabelText("OpenViking Endpoint").parentElement!;
}

async function renderHealth() {
  render(<HealthView active user={{ id: "admin", role: "admin", platform_role: "platform_admin" }} />);
  await waitFor(() => expect(screen.getByLabelText("OpenViking Endpoint")).toHaveValue(
    sharing.endpoint ?? sharing.endpoint_override ?? "",
  ));
}

function report(overrides: Partial<VikingDirsReport> = {}): VikingDirsReport {
  return { action: "ok", endpoint, account_id: "remote-team", existing: ["viking://resources/team"], ...overrides };
}

describe("HealthView OpenViking deployment", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sharing = { deployment: "local", endpoint, account: "remote-team", api_key_present: true };
    storageFails = false;
    failStorageAfterSave = false;
    mockedApi.mockImplementation(async (path, init) => {
      if (path === "/api/sharing-config") {
        if (init?.method === "PUT") {
          const settings = JSON.parse(String(init.body));
          sharing = { ...sharing, ...settings, endpoint: settings.endpoint_override };
          storageFails = failStorageAfterSave;
        }
        return { ...sharing };
      }
      if (path === "/api/storage/status") {
        if (storageFails) throw new Error("status timeout");
        return { backend: "postgres", effective_backend: "postgres", reachable: true, pg: { reachable: true } };
      }
      if (path === "/healthz") return { status: "ok" };
      if (path === "/status") return { running: false };
      if (path === "/api/model-settings") return { model: "test", base_url: "http://model", api_key_present: true };
      if (path === "/api/users") return { users: [{ id: "admin" }] };
      if (path === "/api/skills") return { skills: [], skill_backend: "postgres" };
      if (path === "/api/replay-adapter") return { available: [], configured: false };
      return {};
    });
    mockedCommand.mockResolvedValue({ openviking_dirs: report() });
  });
  afterEach(cleanup);

  it("loads the saved endpoint and keeps PostgreSQL health separate from unchecked OpenViking", async () => {
    await renderHealth();
    expect(endpointStatus()).toHaveTextContent(`生效地址：${endpoint} · 未检查`);
    expect(endpointStatus()).not.toHaveTextContent("可达");
    const storageRow = screen.getByText("对象存储").closest("tr")!;
    expect(within(storageRow).getByText("PostgreSQL · 可达")).toBeVisible();
    expect(screen.getByText("存储后端").parentElement!.parentElement).toHaveTextContent("PostgreSQL");
    expect(mockedCommand).not.toHaveBeenCalled();
  });

  it("also loads the legacy endpoint_override response", async () => {
    sharing = { ...sharing, endpoint: undefined, endpoint_override: "http://legacy.test:1933" };
    await renderHealth();
    expect(endpointStatus()).toHaveTextContent("http://legacy.test:1933 · 未检查");
  });

  it("matches the cloud default endpoint when no endpoint override is saved", async () => {
    const cloudEndpoint = "https://api.vikingdb.cn-beijing.volces.com/openviking";
    sharing = { ...sharing, deployment: "cloud", endpoint: "" };
    mockedCommand.mockResolvedValue({ openviking_dirs: report({ endpoint: cloudEndpoint }) });
    await renderHealth();
    await waitFor(() => expect(endpointStatus()).toHaveTextContent(`生效地址：${cloudEndpoint} · 未检查`));
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent("目录检查通过"));
    expect(toastErr).not.toHaveBeenCalled();
    expect(toastOk).toHaveBeenCalledWith("OpenViking 目录已就绪", expect.any(String));
  });

  it("preserves an existing Key when the password is left blank and does not replace storage status on save", async () => {
    await renderHealth();
    failStorageAfterSave = true;
    fireEvent.change(screen.getByLabelText("OpenViking Account"), { target: { value: "next-account" } });
    fireEvent.click(screen.getByRole("button", { name: "保存部署设置" }));
    await waitFor(() => expect(toastOk).toHaveBeenCalledWith("已保存 OpenViking 设置", "本地自建"));
    const save = mockedApi.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(JSON.parse(String(save[1]!.body))).toEqual({
      deployment: "local", endpoint_override: endpoint, account: "next-account",
    });
    expect(screen.getByText("对象存储").closest("tr")).toHaveTextContent("PostgreSQL · 可达");
    expect(mockedApi.mock.calls.filter(([path]) => path === "/api/sharing-config").length).toBeGreaterThan(2);
    expect(mockedCommand).not.toHaveBeenCalled();
  });

  it("omits a Key that was entered and then cleared before saving", async () => {
    await renderHealth();
    const key = screen.getByLabelText(/OpenViking Root Key/);
    fireEvent.change(key, { target: { value: "replacement-key" } });
    fireEvent.change(key, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存部署设置" }));
    await waitFor(() => expect(toastOk).toHaveBeenCalledWith("已保存 OpenViking 设置", "本地自建"));
    const save = mockedApi.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(JSON.parse(String(save[1]!.body))).not.toHaveProperty("api_key");
    expect(key).toHaveValue("");
  });

  it("uses the real matching directory report and invalidates it for unsaved address or Account edits", async () => {
    await renderHealth();
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent("目录检查通过"));
    expect(mockedCommand).toHaveBeenCalledWith("/api/sharing-config/bootstrap-dirs", expect.objectContaining({
      method: "POST", body: JSON.stringify({ account: "remote-team" }),
    }));

    fireEvent.change(screen.getByLabelText("OpenViking Endpoint"), { target: { value: "http://other.test:1933" } });
    expect(endpointStatus()).toHaveTextContent("待保存地址：http://other.test:1933 · 未检查（请先保存设置）");
    expect(screen.getByRole("button", { name: "检查并补齐目录" })).toBeDisabled();
    expect(screen.getByText(/上次检查（与当前设置不一致）/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(endpointStatus()).toHaveTextContent("目录检查通过");

    fireEvent.change(screen.getByLabelText("OpenViking Account"), { target: { value: "another-account" } });
    expect(endpointStatus()).toHaveTextContent("未检查（请先保存设置）");
    fireEvent.click(screen.getByRole("button", { name: "保存部署设置" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent(`生效地址：${endpoint} · 未检查`));
    expect(screen.queryByText(/上次检查（与当前设置不一致）/)).not.toBeInTheDocument();
  });

  it("expires the old check when replacing a Key at the same endpoint", async () => {
    await renderHealth();
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent("目录检查通过"));
    fireEvent.change(screen.getByLabelText(/OpenViking Root Key/), { target: { value: "new-root-key" } });
    expect(endpointStatus()).toHaveTextContent("未检查（请先保存设置）");
    fireEvent.click(screen.getByRole("button", { name: "保存部署设置" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent(`生效地址：${endpoint} · 未检查`));
    const save = mockedApi.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(JSON.parse(String(save[1]!.body))).toHaveProperty("api_key", "new-root-key");
    expect(screen.getByLabelText(/OpenViking Root Key/)).toHaveValue("");
  });

  it("does not claim an endpoint is checked when the Worker reports a different endpoint or Account", async () => {
    mockedCommand.mockResolvedValue({ openviking_dirs: report({ endpoint: "http://old.test:1933", account_id: "old-account" }) });
    await renderHealth();
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    expect(await screen.findByText(/上次检查（与当前设置不一致）/)).toHaveTextContent("http://old.test:1933");
    expect(endpointStatus()).toHaveTextContent("未检查");
    expect(endpointStatus()).not.toHaveTextContent("目录检查通过");
    expect(toastErr).toHaveBeenCalledWith("目录检查结果与当前设置不一致", expect.stringContaining("http://old.test:1933"));
    expect(toastOk).not.toHaveBeenCalled();
  });

  it("surfaces a partial or failed directory report without a successful readiness toast", async () => {
    mockedCommand.mockResolvedValue({ openviking_dirs: report({
      action: "partial", errors: [{ uri: "viking://resources/team", error: "permission denied" }],
    }) });
    await renderHealth();
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent("部分目录检查失败"));
    expect(toastErr).toHaveBeenCalledWith("目录检查失败", "permission denied");
    expect(toastOk).not.toHaveBeenCalledWith("OpenViking 目录已就绪", expect.anything());

    mockedCommand.mockResolvedValue({ openviking_dirs: report({
      action: "failed", existing: [], error: "OpenViking API key is not configured",
    }) });
    fireEvent.click(screen.getByRole("button", { name: "检查并补齐目录" }));
    await waitFor(() => expect(endpointStatus()).toHaveTextContent("目录检查失败"));
    expect(screen.getByText(/OpenViking API key is not configured/)).toBeVisible();
  });
});
