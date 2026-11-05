// @vitest-environment jsdom
import { api } from "@/api/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { miningApi } from "./miningApi";

const selected = vi.hoisted(() => ({ tenant: "tenant-a" }));
vi.mock("@/api/client", () => ({
  api: vi.fn(),
  tenantHeaders: (_path: string, initial?: HeadersInit) => {
    const headers = new Headers(initial);
    headers.set("X-Tenant-Id", selected.tenant);
    return headers;
  },
}));
const mockedApi = vi.mocked(api);
const receipt = { job_id: "miner-command:1", status_url: "/api/mined-jobs/miner-command:1" };

beforeEach(() => {
  mockedApi.mockReset();
  selected.tenant = "tenant-a";
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("Skill mining Worker commands", () => {
  it("keeps a normal business job response without polling or resubmitting", async () => {
    const job = { job_id: "mining:1", job: { status: "queued" } };
    mockedApi.mockResolvedValue(job);
    expect(await miningApi("/api/mining/jobs", { method: "POST" })).toEqual(job);
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("waits for an accepted upload result and pins all polls to its submitting tenant", async () => {
    mockedApi.mockResolvedValueOnce(receipt)
      .mockImplementationOnce(async () => {
        selected.tenant = "tenant-b";
        return { status: "running" };
      })
      .mockResolvedValueOnce({ status: "succeeded", result: { file_count: 2, source_id: "source-1" } });
    const pending = miningApi("/api/mining/sources/upload", { method: "POST", headers: { "Content-Type": "application/json" } });
    await vi.advanceTimersByTimeAsync(2000);
    expect(await pending).toEqual({ file_count: 2, source_id: "source-1" });
    expect(mockedApi.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    for (const [, init] of mockedApi.mock.calls) {
      expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-a");
    }
  });

  it("pins accepted source-reading jobs to the tenant that requested them", async () => {
    mockedApi.mockResolvedValueOnce(receipt)
      .mockImplementationOnce(async () => {
        selected.tenant = "tenant-b";
        return { status: "running" };
      })
      .mockResolvedValueOnce({ status: "succeeded", result: { sources: [] } });
    const pending = miningApi("/api/mining/ov-sources");
    await vi.advanceTimersByTimeAsync(2000);
    expect(await pending).toEqual({ sources: [] });
    expect(mockedApi).toHaveBeenNthCalledWith(1, "/api/mining/ov-sources");
    for (const [, init] of mockedApi.mock.calls.slice(1)) {
      expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-a");
    }
  });

  it.each(["failed", "cancelled"])("reports %s and releases the pending mutation without a false success", async (status) => {
    mockedApi.mockResolvedValueOnce(receipt).mockResolvedValueOnce({ status, error_code: "SOURCE_UNAVAILABLE" });
    await expect(miningApi("/api/mining/artifacts/content", { method: "POST" })).rejects.toThrow("SOURCE_UNAVAILABLE");
    expect(mockedApi).toHaveBeenCalledTimes(2);
  });

  it("does not treat terminal status alone as a saved artifact", async () => {
    mockedApi.mockResolvedValueOnce(receipt).mockResolvedValueOnce({ status: "succeeded" });
    await expect(miningApi("/api/mining/artifacts/content", { method: "POST" })).rejects.toThrow("结果尚不可用");
  });

  it("refuses a receipt pointing outside this mining API", async () => {
    mockedApi.mockResolvedValueOnce({ ...receipt, status_url: "https://other.example/jobs/1" });
    await expect(miningApi("/api/mining/sources/upload", { method: "POST" })).rejects.toThrow("有效的挖掘后台任务");
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("bounds waiting while preserving the accepted job for later inspection", async () => {
    mockedApi.mockResolvedValueOnce(receipt).mockResolvedValue({ status: "waiting_external" });
    const pending = miningApi("/api/mining/sources/upload", { method: "POST" });
    const rejected = expect(pending).rejects.toThrow("仍在后台执行");
    await vi.advanceTimersByTimeAsync(20 * 60 * 1000);
    await rejected;
    expect(mockedApi.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("stops following accepted work when its caller aborts", async () => {
    const controller = new AbortController();
    const aborted = new Error("caller left page");
    mockedApi.mockResolvedValueOnce(receipt).mockResolvedValue({ status: "running" });
    const pending = miningApi("/api/mining/sources/upload", { method: "POST", signal: controller.signal });
    const rejected = expect(pending).rejects.toThrow("caller left page");
    await vi.advanceTimersByTimeAsync(0);
    controller.abort(aborted);
    await rejected;
    expect(mockedApi).toHaveBeenCalledTimes(2);
  });
});
