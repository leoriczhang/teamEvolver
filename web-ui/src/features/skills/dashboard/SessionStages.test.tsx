// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SessionStages from "./SessionStages";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
afterEach(cleanup);

const stages = [
  { stage: "analyze", status: "succeeded", updated_at: "2026-11-18T08:22:02Z" },
  { stage: "archive", status: "succeeded", updated_at: "2026-11-18T08:28:39Z" },
  { stage: "normalize", status: "succeeded", updated_at: "2026-11-18T08:18:53Z" },
  { stage: "split", status: "succeeded", updated_at: "2026-11-18T08:18:53Z" },
  { stage: "trace_analyze", status: "succeeded", updated_at: "2026-11-18T08:28:38Z" },
];

describe("Session stage timeline", () => {
  beforeEach(() => vi.clearAllMocks());

  it("orders phases and lazily expands real analysis details, then reuses them", async () => {
    mocks.api.mockResolvedValue({
      stage: "analyze", status: "succeeded", detail: { decision: "valuable" }, result_available: true,
      result: { summary: "根据工具错误识别出可复用的改进建议", judge: { overall_score: 0.8 },
        value_judge: { decision: "task_only", mode: "merged" } },
    });
    render(<SessionStages sid="session-1" stages={stages} />);
    expect(screen.getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual([
      "展开标准化阶段", "展开会话切分阶段", "展开会话分析阶段", "展开Trace 分析阶段", "展开归档阶段",
    ]);
    expect(screen.getByText("已完成 5 / 5")).toBeTruthy();
    expect(mocks.api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "展开会话分析阶段" }));
    expect(await screen.findByText("根据工具错误识别出可复用的改进建议")).toBeTruthy();
    expect(screen.getByText("0.80")).toBeTruthy();
    expect(screen.getByText("有价值会话")).toBeTruthy();
    expect(screen.getByText("仅当前任务")).toBeTruthy();
    expect(screen.getByText("联合分析")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledWith("/api/sessions/session-1/stages/analyze", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    fireEvent.click(screen.getByRole("button", { name: "收起会话分析阶段" }));
    expect(screen.queryByText("根据工具错误识别出可复用的改进建议")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开会话分析阶段" }));
    expect(screen.getByText("根据工具错误识别出可复用的改进建议")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledTimes(1);
  });

  it("shows trace findings as readable cards", async () => {
    mocks.api.mockResolvedValue({
      stage: "trace_analyze", result_available: true, result: {
        traces: [
          { trace_id: "trace-1", turn_num: 1, is_badcase: true, problem_description: "重复调用失败工具" },
          { trace_id: "trace-2", turn_num: 2, is_badcase: false, problem_description: "" },
        ],
      },
    });
    render(<SessionStages sid="session-1" stages={stages} />);
    fireEvent.click(screen.getByRole("button", { name: "展开Trace 分析阶段" }));
    expect(await screen.findByText("重复调用失败工具")).toBeTruthy();
    expect(screen.getByText("发现问题")).toBeTruthy();
    expect(screen.getByText("未发现问题")).toBeTruthy();
    expect(screen.getByText("trace-2")).toBeTruthy();
  });

  it("keeps stage errors visible, supports retry, and distinguishes unavailable outputs", async () => {
    mocks.api.mockRejectedValueOnce(new Error("暂时不可用")).mockResolvedValueOnce({
      stage: "analyze", status: "failed", error_code: "TraceAnalysisError",
      result_available: false, result_unavailable_reason: "该阶段尚未保存产物", detail: {},
    });
    render(<SessionStages sid="session-1" stages={[{ stage: "analyze", status: "failed", error_code: "TraceAnalysisError" }]} />);
    expect(screen.getByText("失败 1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开会话分析阶段" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("错误代码：TraceAnalysisError")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("该阶段尚未保存产物")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledTimes(2);
  });

  it("does not leak expanded data into another session", async () => {
    mocks.api.mockResolvedValue({
      stage: "analyze", result_available: true, result: { summary: "第一个会话的分析" },
    });
    const view = render(<SessionStages sid="first" stages={stages} />);
    fireEvent.click(screen.getByRole("button", { name: "展开会话分析阶段" }));
    expect(await screen.findByText("第一个会话的分析")).toBeTruthy();
    view.rerender(<SessionStages sid="second" stages={stages} />);
    expect(screen.queryByText("第一个会话的分析")).toBeNull();
    expect(screen.getByRole("button", { name: "展开会话分析阶段" }).getAttribute("aria-expanded")).toBe("false");
    await waitFor(() => expect(mocks.api).toHaveBeenCalledTimes(1));
  });
});
