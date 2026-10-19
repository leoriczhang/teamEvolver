// @vitest-environment jsdom
import { api } from "@/api/client";
import ExperienceSyncPanel from "@/features/skills/ExperienceSyncPanel";
import "@testing-library/jest-dom/vitest";
import { act,cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);
const status = {
  enabled: true, target_directory: "viking://resources/custom/current-tenant",
  counts: { pending: 0, synced: 2, retry: 0, dead_letter: 0 }, last_scan: null, last_error: null, manual: null,
};
const receipt = { job_id: "request-1", status_url: "/api/jobs/request-1" };
const manual = { request_id: "request-1", state: "queued", requested_at: 1800000000 };

describe("ExperienceSyncPanel", () => {
  beforeEach(() => { mockedApi.mockReset(); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("requires enabled status and explains the formal Session scope", async () => {
    mockedApi.mockResolvedValue({ ...status, enabled: false });
    render(<ExperienceSyncPanel active />);
    expect(await screen.findByText(/当前租户未开启经验同步/)).toBeVisible();
    expect(screen.getByRole("button", { name: "立即同步到 OV" })).toBeDisabled();
    expect(screen.getByText(/全部已入库 Session 分析/)).toBeVisible();
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("accepts a durable job and prevents duplicate clicks", async () => {
    let finish!: (value: typeof receipt) => void;
    mockedApi.mockImplementation((path) => path.endsWith("trigger")
      ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(status));
    render(<ExperienceSyncPanel active />);
    const button = screen.getByRole("button", { name: "立即同步到 OV" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(mockedApi.mock.calls.filter(([path]) => path.endsWith("trigger"))).toEqual([
      ["/api/experience-sync/trigger", { method: "POST" }],
    ]);
    await act(async () => finish(receipt));
    expect(await screen.findByRole("status")).toHaveTextContent("已受理，后台处理中");
    expect(button).toBeDisabled();
    expect(screen.queryByText(/本轮核对结束/)).not.toBeInTheDocument();
  });

  it("separates completed scanning from retrying and dead-letter deliveries", async () => {
    vi.useFakeTimers();
    mockedApi.mockResolvedValueOnce({ ...status, manual }).mockResolvedValue({
      ...status, counts: { pending: 0, synced: 2, retry: 1, dead_letter: 1 }, last_error: "OV_NETWORK_ERROR",
      index_pending: 1, manual: { ...manual, state: "completed", finished_at: 1800000001 },
      retry: { due: 0, deferred: 1, next_attempt_at: 1800000090 },
    });
    render(<ExperienceSyncPanel active />);
    await act(async () => {});
    expect(screen.getByRole("status")).toHaveTextContent("后台处理中");
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("status")).toHaveTextContent("本轮核对结束");
    expect(screen.getByText(/等待重试 1/)).toBeVisible();
    expect(screen.getByText(/等待退避 1 条/)).toBeVisible();
    expect(screen.getByText(/正文已写入、索引待确认/)).toHaveTextContent("1 条");
    expect(screen.getByText(/部分投递已停止重试/)).toBeVisible();
    expect(screen.getByRole("button", { name: "立即同步到 OV" })).toBeEnabled();
  });

  it("shows submit errors without claiming acceptance", async () => {
    mockedApi.mockImplementation(path => path.endsWith("trigger")
      ? Promise.reject(new Error("SYNC_ALREADY_RUNNING")) : Promise.resolve(status));
    render(<ExperienceSyncPanel active />);
    const button = screen.getByRole("button", { name: "立即同步到 OV" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("已有同步正在执行");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not fetch while inactive or show another tenant's late response", async () => {
    let finish!: (value: typeof status) => void;
    mockedApi.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const view = render(<ExperienceSyncPanel key="old" active />);
    view.rerender(<ExperienceSyncPanel key="new" active={false} />);
    await act(async () => finish(status));
    expect(screen.queryByText(/custom\/current-tenant/)).not.toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("imports history through the dedicated async endpoint", async () => {
    mockedApi.mockImplementation(path => path.endsWith("/import") ? Promise.resolve(receipt) : Promise.resolve(status));
    render(<ExperienceSyncPanel active />);
    const button = screen.getByRole("button", { name: "导入存量成功经验" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith("/api/experience-sync/import", { method: "POST" }));
    expect(await screen.findByRole("status")).toHaveTextContent("已受理");
  });

  it("shows a failed scan distinctly from successful completion", async () => {
    mockedApi.mockResolvedValue({ ...status, manual: { ...manual, state: "failed", error_code: "source_error" } });
    render(<ExperienceSyncPanel active />);
    expect(await screen.findByRole("status")).toHaveTextContent("本轮核对失败");
    expect(screen.queryByText(/本轮核对结束/)).not.toBeInTheDocument();
  });
});
