// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import ExperienceSyncPanel from "./ExperienceSyncPanel";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);
const status = {
  enabled: true, target_directory: "viking://resources/custom/current-tenant",
  counts: { pending: 0, synced: 2, retry: 0 }, last_scan: null, last_error: null, manual: null,
};
const receipt = { manual: { request_id: "request-1", state: "queued", requested_at: 1800000000 } };

describe("ExperienceSyncPanel", () => {
  beforeEach(() => { mockedApi.mockReset(); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("requires enabled status and explains the legacy JSON scope", async () => {
    mockedApi.mockResolvedValue({ ...status, enabled: false });
    render(<ExperienceSyncPanel active />);
    expect(await screen.findByText(/当前租户未开启经验同步/)).toBeVisible();
    expect(screen.getByRole("button", { name: "立即同步到 OV" })).toBeDisabled();
    expect(screen.getByText(/全部 Session 分析和历史经验/)).toBeVisible();
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("submits without identity overrides and prevents duplicate clicks", async () => {
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

  it("polls durable status and distinguishes completed passes from retrying items", async () => {
    vi.useFakeTimers();
    mockedApi.mockResolvedValueOnce({ ...status, ...receipt }).mockResolvedValue({
      ...status, counts: { pending: 0, synced: 2, retry: 1 }, last_error: "OV_TIMEOUT",
      manual: { ...receipt.manual, state: "completed", finished_at: 1800000001 },
    });
    render(<ExperienceSyncPanel active />);
    await act(async () => {});
    expect(screen.getByRole("status")).toHaveTextContent("后台处理中");
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("status")).toHaveTextContent("本轮核对结束");
    expect(screen.getByText(/等待重试 1/)).toBeVisible();
    expect(screen.getByText(/OV_TIMEOUT/)).toBeVisible();
    expect(screen.getByRole("button", { name: "立即同步到 OV" })).toBeEnabled();
  });

  it("shows busy errors without claiming acceptance", async () => {
    mockedApi.mockImplementation(path => path.endsWith("trigger")
      ? Promise.reject(new Error("SYNC_ALREADY_RUNNING")) : Promise.resolve(status));
    render(<ExperienceSyncPanel active />);
    const button = screen.getByRole("button", { name: "立即同步到 OV" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("已有同步正在执行");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not fetch while inactive or render stale results after a tenant remount", async () => {
    let finish!: (value: typeof status) => void;
    mockedApi.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const view = render(<ExperienceSyncPanel key="old" active />);
    view.rerender(<ExperienceSyncPanel key="new" active={false} />);
    await act(async () => finish(status));
    expect(screen.queryByText(/custom\/current-tenant/)).not.toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });
});

it("imports all history using the dedicated endpoint and shows coverage gaps", async () => {
  const result = { manual: {
    ...receipt.manual, operation: "import_all", state: "completed", finished_at: 1800000001,
    import: { phase: "complete", processed_sources: 12001, eligible_records: 11001,
      prepared_documents: 201, rejected_sources: 1, last_error: "IMPORT_SOURCE_TOO_LARGE" },
  } };
  mockedApi.mockReset();
  mockedApi.mockImplementation(path => path.endsWith("/import") ? Promise.resolve(result) : Promise.resolve(status));
  const view = render(<ExperienceSyncPanel active />);
  try {
    const button = screen.getByRole("button", { name: "导入存量成功经验" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith("/api/experience-sync/import", { method: "POST" }));
    expect(await screen.findByText(/已扫描 12001 条来源/)).toHaveTextContent("准备 201 条去重文档");
    expect(screen.getByText(/存量导入存在缺口/)).toHaveTextContent("IMPORT_SOURCE_TOO_LARGE");
  } finally {
    view.unmount();
  }
});

it("shows partial completion, per-source gaps and separate index pending counts", async () => {
  mockedApi.mockReset();
  mockedApi.mockResolvedValue({ ...status, index_pending: 2, manual: {
    ...receipt.manual, operation: "import_all", state: "completed", finished_at: 1800000001,
    import: { phase: "complete", processed_sources: 20, eligible_records: 73, prepared_documents: 73,
      rejected_sources: 2, rejected_records: 1, last_error: "IMPORT_RECORD_TOO_LARGE",
      error_counts: { IMPORT_SOURCE_TOO_LARGE: 1, IMPORT_RECORD_TOO_LARGE: 1 },
      errors_sample: [{ source_key: "session_archive/large.json", code: "IMPORT_SOURCE_TOO_LARGE",
        source_bytes: 80000000, limit_bytes: 67108864 }, { source_key: "skill_evidence/report.json",
        code: "IMPORT_RECORD_TOO_LARGE", record_bytes: 300000, limit_bytes: 262144, ordinal: 21 }],
    },
  } });
  const view = render(<ExperienceSyncPanel active />);
  try {
    expect(await screen.findByRole("status")).toHaveTextContent("部分完成");
    expect(screen.getByText(/正文已写入、索引待确认/)).toHaveTextContent("2 条");
    expect(screen.getByText(/session_archive\/large.json/)).toHaveTextContent("80000000 字节");
    expect(screen.getByText(/skill_evidence\/report.json/)).toHaveTextContent("第 21 条");
    expect(screen.getByText(/来源读取缺口/)).toHaveTextContent("2 个来源");
  } finally { view.unmount(); }
});


it("distinguishes backoff from a blocked scheduler and displays failure stages", async () => {
  mockedApi.mockReset();
  mockedApi.mockResolvedValue({ ...status, counts: { pending: 0, synced: 0, retry: 73 },
    last_error: "CONFLICT", last_error_at: 1800000000,
    worker: { state: "blocked", scheduler_running: true, error: "SYNC_STORAGE_FAILURE" },
    last_delivery_pass: { finished_at: 1800000100, attempted: 0, deferred: 73 },
    retry: { due: 0, deferred: 73, next_attempt_at: 1800000900, last_attempt_at: 1800000000,
      error_counts: { CONFLICT: 73 }, samples: [{ uri: "viking://resources/example.json", code: "CONFLICT",
        attempts: 10, next_attempt_at: 1800000900,
        failure: { stage: "write", path: "/api/v1/content/write", status: 409, ov_request_id: "ov-example" } }] },
  });
  const view = render(<ExperienceSyncPanel active />);
  try {
    expect(await screen.findByText(/等待退避 73 条/)).toBeVisible();
    expect(screen.getByText(/后台执行状态/)).toHaveTextContent("执行受阻");
    expect(screen.getByText(/最近上传检查/)).toHaveTextContent("本批尝试 0 条");
    expect(screen.getByText(/最近失败尝试/)).toBeVisible();
    expect(screen.getByText(/viking:\/\/resources\/example.json/)).toHaveTextContent("阶段 write");
    expect(screen.getByText(/viking:\/\/resources\/example.json/)).toHaveTextContent("OV request ID ov-example");
  } finally { view.unmount(); }
});
