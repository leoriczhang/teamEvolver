// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DatasetsView from "./DatasetsView";
import DatasetSelectionDialog from "./datasets/DatasetSelectionDialog";
import { api } from "@/api/client";

vi.mock("@/api/client", () => ({ api: vi.fn(), tenantHeaders: () => new Headers() }));
vi.mock("@/lib/toast", () => ({ toastOk: vi.fn(), toastErr: vi.fn() }));
const dataset = { dataset_id: "ds_test", name: "测试数据集", description: "失败案例", item_count: 1, created_at: "2026-09-18T10:00:00Z" };
const item = { item_id: "item_test", session_id: "session_test", trace_id: "trace_original", query: "整理客户报告", requirements: ["生成报告"], used_skills: [], timestamp: "2026-09-15T01:00:00Z", ingested_at: "2026-09-18T01:00:00Z" };
const batch = { run_id: "batch_test", dataset_id: "ds_test", status: "completed", total: 1, completed: 1, succeeded: 0, failed: 1, skipped: 0, created_at: "2026-09-18T10:00:00Z", items: [{ ...item, status: "completed", success: false, replay_trace_id: "replay_test" }] };

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/datasets/ds_test/runs/batch_test/results/item_test") return { ok: true, completed: false, final_response: "未能生成报告", checklist_report: { judge: "model", all_satisfied: false } };
    if (path.startsWith("/api/datasets/ds_test/runs/batch_test")) return batch;
    if (path.startsWith("/api/datasets/ds_test/runs?")) return { runs: [batch], total: 1 };
    if (path.startsWith("/api/datasets/ds_test?")) return { ...dataset, items: [item], total: 1 };
    if (path.startsWith("/api/datasets?")) return { datasets: [dataset], total: 1 };
    return {};
  });
});
afterEach(cleanup);

describe("dataset console", () => {
  it("opens the saved collection and distinguishes completed execution from passing Checklist", async () => {
    render(<DatasetsView active />);
    fireEvent.click(await screen.findByRole("button", { name: "测试数据集" }));
    expect(await screen.findByRole("button", { name: "导出 ZIP" })).toBeEnabled();
    expect(await screen.findByText("未通过")).toBeInTheDocument();
    expect(screen.getAllByText("完成").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "结果" }));
    expect(await screen.findByText("未能生成报告")).toBeInTheDocument();
    expect(screen.getByText("Checklist 未通过")).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("dataset")).toBe("ds_test");
  });

  it("submits a background batch and prevents repeat starts while it runs", async () => {
    const original = vi.mocked(api).getMockImplementation()!;
    vi.mocked(api).mockImplementation(async (path: string, opts?: RequestInit) => {
      if (path === "/api/datasets/ds_test/runs" && opts?.method === "POST") return { ...batch, status: "running", completed: 0 };
      if (path.startsWith("/api/datasets/ds_test/runs")) {
        if (path.includes("batch_test")) return { ...batch, status: "running", completed: 0 };
        return { runs: [], total: 0 };
      }
      return original(path, opts);
    });
    render(<DatasetsView active />);
    fireEvent.click(await screen.findByRole("button", { name: "测试数据集" }));
    fireEvent.click(await screen.findByRole("button", { name: "批量重回放" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "开始重回放" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api).toHaveBeenCalledWith("/api/datasets/ds_test/runs", expect.objectContaining({ method: "POST", body: JSON.stringify({ concurrency: 2, timeout_seconds: 600, max_interactions: 4 }) }));
    expect(screen.getByRole("button", { name: "批量重回放" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  });

  it("saves exactly the selected Session IDs from the overview", async () => {
    vi.mocked(api).mockResolvedValue(dataset);
    const saved = vi.fn();
    render(<DatasetSelectionDialog selection={{ session_ids: ["session-a", "session-b"] }} selectionCount={2} onClose={() => {}} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText("数据集名称"), { target: { value: "导出回归集" } });
    fireEvent.click(screen.getByRole("button", { name: "创建数据集" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(dataset));
    expect(api).toHaveBeenCalledWith("/api/datasets", expect.objectContaining({ body: JSON.stringify({ name: "导出回归集", description: "", session_ids: ["session-a", "session-b"] }) }));
    expect(api).not.toHaveBeenCalledWith(expect.stringContaining("/conversations"), expect.anything());
  });
});
