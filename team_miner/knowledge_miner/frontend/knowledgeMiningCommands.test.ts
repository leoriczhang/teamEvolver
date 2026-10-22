// @vitest-environment jsdom
import { api } from "@/api/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelKnowledgeTask, loadKnowledgeTask, submitKnowledgeTask, waitForKnowledgeCommand,
} from "./knowledgeMiningCommands";

const selected = vi.hoisted(() => ({ tenant: "tenant-a" }));
vi.mock("@/api/client", () => ({
  api: vi.fn(),
  tenantHeaders: (_path: string, initial?: HeadersInit) => {
    const headers = new Headers(initial);
    if (!headers.has("X-Tenant-Id")) headers.set("X-Tenant-Id", selected.tenant);
    return headers;
  },
}));
const mockedApi = vi.mocked(api);
const receipt = { job_id: "miner:upload-1", status_url: "/api/jobs/miner:upload-1" };

beforeEach(() => {
  mockedApi.mockReset();
  selected.tenant = "tenant-a";
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("Knowledge mining Worker commands", () => {
  it("keeps a legacy synchronous response without polling", async () => {
    const result = { html: "<p>Wiki</p>" };
    expect(await waitForKnowledgeCommand(result)).toBe(result);
    expect(mockedApi).not.toHaveBeenCalled();
  });

  it("pins polls and the separate result request to the submitting tenant", async () => {
    selected.tenant = "tenant-b";
    mockedApi.mockResolvedValueOnce({ status: "running" })
      .mockResolvedValueOnce({ status: "succeeded" })
      .mockResolvedValueOnce({ files: [{ uri: "viking://resources/source/a.md" }] });
    const pending = waitForKnowledgeCommand(receipt, { headers: { "X-Tenant-Id": "tenant-a" } });
    await vi.advanceTimersByTimeAsync(2000);
    expect(await pending).toEqual({ files: [{ uri: "viking://resources/source/a.md" }] });
    expect(mockedApi.mock.calls.map(([path]) => path)).toEqual([
      receipt.status_url, receipt.status_url, "/api/knowledge-mining/jobs/miner:upload-1/result",
    ]);
    for (const [, init] of mockedApi.mock.calls) {
      expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-a");
    }
  });

  it.each(["https://other.test/job/1", "/api/jobs/other", ""])("rejects an invalid receipt URL %s", async (statusUrl) => {
    await expect(waitForKnowledgeCommand({ ...receipt, status_url: statusUrl })).rejects.toThrow("有效的知识库后台任务");
    expect(mockedApi).not.toHaveBeenCalled();
  });

  it.each(["failed", "cancelled"])("reports terminal %s without fetching a successful result", async (status) => {
    mockedApi.mockResolvedValue({ status, error_code: "SOURCE_UNAVAILABLE" });
    await expect(waitForKnowledgeCommand(receipt)).rejects.toThrow("SOURCE_UNAVAILABLE");
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("allows successful deletion without requesting a nonexistent result", async () => {
    mockedApi.mockResolvedValue({ status: "succeeded" });
    expect(await waitForKnowledgeCommand(receipt, undefined, false)).toEqual({ ok: true });
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });

  it("bounds waiting without resubmitting the accepted operation", async () => {
    mockedApi.mockResolvedValue({ status: "waiting_external" });
    const pending = waitForKnowledgeCommand(receipt);
    const rejected = expect(pending).rejects.toThrow("仍在后台执行");
    await vi.advanceTimersByTimeAsync(20 * 60 * 1000);
    await rejected;
    expect(mockedApi.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  });

  it("stops following work when its caller aborts", async () => {
    const controller = new AbortController();
    mockedApi.mockResolvedValue({ status: "running" });
    const pending = waitForKnowledgeCommand(receipt, { signal: controller.signal });
    const rejected = expect(pending).rejects.toThrow("left page");
    await vi.advanceTimersByTimeAsync(0);
    controller.abort(new Error("left page"));
    await rejected;
    expect(mockedApi).toHaveBeenCalledTimes(1);
  });
});

describe("Knowledge mining task identities", () => {
  it("returns the accepted local task immediately without waiting for Compile", async () => {
    mockedApi.mockResolvedValue(receipt);
    expect(await submitKnowledgeTask({ account: "demo", instruction: "Build a Wiki" })).toEqual({
      task_id: "miner:upload-1", status: "queued",
    });
    expect(mockedApi).toHaveBeenCalledTimes(1);
    expect(mockedApi).toHaveBeenCalledWith("/api/knowledge-mining/tasks", expect.objectContaining({ method: "POST" }));
  });

  it.each(["queued", "running", "cancelled"])("keeps a local %s task identity until an upstream task exists", async (status) => {
    mockedApi.mockResolvedValue({ job_id: receipt.job_id, status });
    expect(await loadKnowledgeTask(receipt.job_id, "demo")).toMatchObject({ task_id: receipt.job_id, status });
    expect(mockedApi).toHaveBeenCalledTimes(1);
    expect(mockedApi.mock.calls[0][0]).toBe(receipt.status_url);
  });

  it("follows the upstream identity with the same tenant and preserves the local job association", async () => {
    mockedApi.mockImplementationOnce(async () => {
      selected.tenant = "tenant-b";
      return { status: "running", upstream_task_id: "ov-task-1" };
    }).mockResolvedValueOnce({ task: { task_id: "ov-task-1", status: "succeeded" } });
    expect(await loadKnowledgeTask(receipt.job_id, "demo")).toMatchObject({
      task_id: "ov-task-1", job_id: receipt.job_id, status: "completed",
    });
    expect(mockedApi.mock.calls[1][0]).toBe("/api/knowledge-mining/tasks/ov-task-1?account=demo");
    for (const [, init] of mockedApi.mock.calls) {
      expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-a");
    }
  });

  it("loads a legacy upstream task directly and normalizes success", async () => {
    mockedApi.mockResolvedValue({ task: { task_id: "ov-task-1", status: "succeeded" } });
    expect(await loadKnowledgeTask("ov-task-1", "account name")).toMatchObject({ status: "completed" });
    expect(mockedApi.mock.calls[0][0]).toBe("/api/knowledge-mining/tasks/ov-task-1?account=account%20name");
  });

  it("cancels a local job through the Worker API", async () => {
    mockedApi.mockResolvedValue({ job_id: receipt.job_id, status: "running", cancel_requested: true });
    expect(await cancelKnowledgeTask(receipt.job_id, "demo")).toMatchObject({
      task_id: receipt.job_id, status: "cancelling",
    });
    expect(mockedApi.mock.calls[0][0]).toBe("/api/jobs/miner:upload-1/cancel");
  });

  it("cancels a legacy task through its upstream route", async () => {
    mockedApi.mockResolvedValue({ task: { task_id: "ov-task-1", status: "cancelled" } });
    expect(await cancelKnowledgeTask("ov-task-1", "demo")).toMatchObject({ status: "cancelled" });
    expect(mockedApi).toHaveBeenCalledWith("/api/knowledge-mining/tasks/ov-task-1/cancel", expect.objectContaining({
      method: "POST", body: JSON.stringify({ account: "demo" }),
    }));
  });
});
