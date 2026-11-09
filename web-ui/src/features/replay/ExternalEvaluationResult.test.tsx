// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import ExternalEvaluationResult from "./ExternalEvaluationResult";

afterEach(cleanup);

it("shows paired business scores and reasoning without interpreting Tokens as the selected criterion", () => {
  render(<ExternalEvaluationResult report={{ status: "evaluated", case_count: 5,
    baseline: { score: 62.5, reason: "原输出存在事实遗漏" }, candidate: { score: 80, reason: "修复事实遗漏" }, delta: 17.5,
  }} />);
  expect(screen.getByRole("table", { name: "A/B 外部评分对比" })).toBeVisible();
  for (const value of ["62.5", "80", "17.5", "修复事实遗漏", "原输出存在事实遗漏"]) expect(screen.getByText(value)).toBeVisible();
  expect(screen.getByText(/5 个 Case 平均分/)).toBeVisible();
  expect(screen.getByText(/此模式按外部评分判断/)).toBeVisible();
});

it("distinguishes unavailable scores from a real zero and never invents a pass", () => {
  const view = render(<ExternalEvaluationResult report={{ status: "unavailable", error: "HTTP 超时" }} />);
  expect(screen.getByText(/HTTP 超时/)).toBeVisible();
  expect(screen.queryByRole("table")).toBeNull();
  view.rerender(<ExternalEvaluationResult report={{ status: "evaluated", baseline: { score: 0 }, candidate: { score: 0 }, delta: 0 }} />);
  expect(screen.getAllByText("0")).toHaveLength(3);
  expect(screen.queryByText("通过")).toBeNull();
});

it("leaves original-mode historical results unchanged and labels missing external evaluations", () => {
  const view = render(<ExternalEvaluationResult policy={{ policy: "progressive-checklist-then-turn-priority" }} />);
  expect(screen.queryByRole("region")).toBeNull();
  view.rerender(<ExternalEvaluationResult policy={{ policy: "external-agent-score" }} />);
  expect(screen.getByText(/未获得完整评分，本次无法确定/)).toBeVisible();
});

it("shows the frozen aggregate score contract and its adoption thresholds", () => {
  render(<ExternalEvaluationResult report={{
    baseline_score: 72, candidate_score: 81.5, score_delta: 9.5, case_count: 4, evaluated_case_count: 4,
    min_score: 80, min_improvement: 5, decision_basis: "external_score_improved",
  }} />);
  expect(screen.getByText("81.5")).toBeVisible();
  expect(screen.getByText("9.5")).toBeVisible();
  expect(screen.getByText(/候选平均分提升，并达到配置/)).toBeVisible();
  expect(screen.getByText(/本次冻结门槛：候选平均分至少 80；提升至少 5/)).toBeVisible();
});

it("does not display a partial aggregate as a complete score comparison", () => {
  render(<ExternalEvaluationResult report={{
    baseline_score: 72, candidate_score: 90, score_delta: 18, case_count: 4, evaluated_case_count: 3,
    decision_basis: "external_evaluation_unavailable",
  }} />);
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.getByText("评分不完整，本次无法确定。")).toBeVisible();
});
