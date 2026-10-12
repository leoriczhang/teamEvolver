// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { EvalResult } from "@/api/client";
import CandidateModal from "./CandidateModal";

vi.mock("./CandidateFeedback", () => ({ default: () => null }));
vi.mock("@/components/UnifiedDiffView", () => ({ default: () => null, InlineDiffView: () => null }));
vi.mock("@/components/common", async (original) => ({ ...await original<typeof import("@/components/common")>(), InfoTip: () => null }));
afterEach(cleanup);

const unknownDimension = { baseline: "unavailable", candidate: "unavailable", delta: "unavailable", reduction_ratio: "unavailable", winner: "unavailable" } as const;
function show(evaluation: EvalResult) {
  const onEvaluate = vi.fn();
  render(<CandidateModal jobId="mined-1" cand={{ job_id: "mined-1", skill_name: "mined-skill" }} ev={evaluation}
    evaluating={false} open onClose={vi.fn()} onEvaluate={onEvaluate} onFeedbackSaved={vi.fn()} />);
  return onEvaluate;
}

it("preserves unsupported replay reason without inventing missing dataset or Session facts, and offers retry", () => {
  const onEvaluate = show({ status: "evaluated", cached: true, recommended_publish: false, replay: {
    status: "skipped", case_count: 0, reason: "ReplayUnsupported: no configured runtime",
    efficiency: { dimensions: { interaction_turns: unknownDimension, tool_call_count: unknownDimension, total_tokens: unknownDimension } },
  } });
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText("已跳过")).toBeVisible();
  expect(within(dialog).getByText("ReplayUnsupported: no configured runtime")).toBeVisible();
  expect(within(dialog).getByText("未完成回放")).toBeVisible();
  expect(within(dialog).getByText("True Replay 尚未完成，暂无 Checklist 判定。")).toBeVisible();
  expect(dialog.textContent).not.toMatch(/NaN|持平|测试集未生成|未采样到|门禁仅参考/);
  expect(within(dialog).getAllByText("未观测").length).toBeGreaterThan(0);
  expect(screen.getByRole("region", { name: /A\/B 效率对比/ })).toHaveAccessibleName(expect.stringContaining("未观测"));
  fireEvent.click(screen.getByRole("button", { name: "重新评估（重跑回放）" }));
  expect(onEvaluate).toHaveBeenCalledWith(true);
});

it("keeps observed zeros and unknown metrics distinct in a completed replay", () => {
  const zero = { baseline: 0, candidate: 0, delta: 0, reduction_ratio: 0, winner: "tie" } as const;
  show({ status: "completed", replay: { case_count: 1, efficiency: { dimensions: {
    tool_call_count: zero, total_tokens: { ...unknownDimension, baseline: Number.NaN },
  } } } });
  const region = screen.getByRole("region", { name: /A\/B 效率对比/ });
  const zeroRow = within(region).getByText("工具调用").closest("tr")!;
  expect(within(zeroRow).getAllByText("0")).toHaveLength(2);
  expect(within(zeroRow).getAllByText("持平")).toHaveLength(2);
  const unknownRow = within(region).getByText("Tokens").closest("tr")!;
  expect(within(unknownRow).getAllByText("未观测")).toHaveLength(4);
  expect(unknownRow).not.toHaveTextContent(/NaN|持平/);
  expect(screen.getByText("指标未观测")).toBeVisible();
  expect(screen.getByText("本次回放未提供 Checklist 判定，无法据此确认完成条件。")).toBeVisible();
});

it("does not show stale efficiency values as tied after a failed replay", () => {
  show({ status: "failed", replay: { reason: "isolated branch failed", efficiency: { dimensions: {
    interaction_turns: { baseline: 0, candidate: 0, delta: 0, reduction_ratio: 0, winner: "tie" },
  } } } });
  expect(screen.getByRole("dialog")).not.toHaveTextContent(/持平|NaN/);
  expect(screen.getByText("回放失败")).toBeVisible();
});
