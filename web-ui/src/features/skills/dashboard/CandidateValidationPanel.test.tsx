// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import CandidateValidationPanel from "./CandidateValidationPanel";

afterEach(cleanup);

describe("CandidateValidationPanel", () => {
  it("shows the standard Skill policy without treating unrequested enhanced checks as missing", () => {
    render(<CandidateValidationPanel plan={{ validation_strategy: "standard-v1", required_skill_ids: [] }} />);
    expect(screen.getByText("Skill 默认验证策略")).toBeVisible();
    expect(screen.getByText("盲测／锁定验收：非必需")).toBeVisible();
    expect(screen.getByText("稳定性／消融门禁：非必需")).toBeVisible();
    expect(screen.queryByText(/验收阻断：|探测阻断：/)).not.toBeInTheDocument();
  });

  it("preserves old candidates without execution validation metadata", () => {
    const { container } = render(<CandidateValidationPanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows locked holdout coverage and every diagnostic variant without calling them business gains", () => {
    render(<CandidateValidationPanel plan={{
      required_skill_ids: ["report", "review"],
      holdout: { status: "ready", datasets: [{ dataset_id: "blind-dataset", revision: 2 }], case_ids: ["blind-one"] },
    }} probe={{
      status: "passed", total_replay_tokens: 12000, token_budget: 1000000, attribution: "single_change_comparisons",
      runs: ["aa", "repeat-1", "only-skill", "only-agent_prompt", "only-context_fragment"].map(kind => ({
        kind, verdict: "inconclusive", execution_gate: { status: "passed" },
      })),
    }} />);
    expect(screen.getByText("盲测／锁定验收：已锁定")).toBeVisible();
    expect(screen.getByText("锁定 Dataset：blind-dataset（v2）")).toBeVisible();
    expect(screen.getByText("共享回归范围：report、review")).toBeVisible();
    expect(screen.getByText("本次 Agent Replay Token（含主验证与探测）：12000 / 本次 Replay 预算 1000000")).toBeVisible();
    expect(screen.getByText(/A\/A 同资产对照/)).toBeVisible();
    expect(screen.getByText(/重复运行 1/)).toBeVisible();
    for (const name of ["仅 Skill", "仅业务 Prompt", "仅上下文注入"]) expect(screen.getByText(new RegExp(`单项消融：${name}`))).toBeVisible();
    expect(screen.getByText(/不代表候选已经带来业务收益/)).toBeVisible();
  });

  it("reports holdout and repeated-run blockers and keeps missing costs unknown", () => {
    render(<CandidateValidationPanel plan={{ holdout: { status: "inconclusive", reasons: ["holdout_source_overlap:dataset-one"] } }}
      probe={{ status: "inconclusive", total_replay_tokens: null, token_budget: 1000,
        reasons: ["repeat_completion_unstable", "probe_cost_unknown"], attribution: "single_module_or_combination_only" }} />);
    expect(screen.getByText("验收阻断：holdout_source_overlap:dataset-one")).toBeVisible();
    expect(screen.getByText("探测阻断：重复运行的完成结果不稳定")).toBeVisible();
    expect(screen.getByText("探测阻断：探测运行的 Token 成本未知")).toBeVisible();
    expect(screen.getByText("本次 Agent Replay Token（含主验证与探测）：未知 / 本次 Replay 预算 1000")).toBeVisible();
    expect(screen.getByText(/尚无单项消融归因/)).toBeVisible();
  });

  it("blocks legacy passed costs while preserving known tokens as a lower bound", () => {
    const cost = {
      status: "passed", known_total_tokens: 261119, budget_tokens: 1000000,
      agent_replay_tokens: 163064, te_llm_usage: { known_total_tokens: 98055, unknown_attempts: 0, complete: true },
    };
    render(<CandidateValidationPanel cost={cost} />);
    expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
    expect(screen.queryByText("候选累计预算检查：通过")).not.toBeInTheDocument();
    expect(screen.getByText("已知累计 Token（去重）：261119 / 候选预算 1000000")).toBeVisible();
    expect(screen.getByText("累计 Agent Replay Token：163064")).toBeVisible();
    expect(screen.getByText(/已知 Token 仅为成本下界/)).toBeVisible();
    expect(screen.getByText("成本阻断：历史裁判／反馈成本未完整采集，不能确认总成本满足预算")).toBeVisible();
    expect(cost.status).toBe("passed");
  });

  it("blocks explicitly incomplete accounting and deduplicates its translated reason", () => {
    render(<CandidateValidationPanel cost={{
      status: "passed", replay_te_llm_accounting_complete: false,
      known_total_tokens: 0, budget_tokens: 1000, te_llm_usage: { unknown_attempts: 0 },
      reasons: ["replay_te_llm_accounting_unverified"],
    }} />);
    expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
    expect(screen.queryByText("候选累计预算检查：通过")).not.toBeInTheDocument();
    expect(screen.getByText("已知累计 Token（去重）：0 / 候选预算 1000")).toBeVisible();
    expect(screen.getAllByText("成本阻断：历史裁判／反馈成本未完整采集，不能确认总成本满足预算")).toHaveLength(1);
  });

  it("preserves a passed gate when Replay judge and feedback accounting is complete", () => {
    render(<CandidateValidationPanel cost={{
      status: "passed", replay_te_llm_accounting_complete: true,
      accounting_status: "complete", total_tokens: 42000, known_total_tokens: 42000, budget_tokens: 100000,
    }} />);
    expect(screen.getByText("候选累计预算检查：通过")).toBeVisible();
    expect(screen.getByText("已知累计 Token（去重）：42000 / 候选预算 100000")).toBeVisible();
    expect(screen.getByText("累计总 Token：42000")).toBeVisible();
    expect(screen.getByText("累计统计：完整")).toBeVisible();
    expect(screen.queryByText(/已知 Token 仅为成本下界/)).not.toBeInTheDocument();
    expect(screen.queryByText(/历史裁判／反馈成本未完整采集/)).not.toBeInTheDocument();
  });

  it("distinguishes an explicitly unlimited budget from missing budget metadata", () => {
    const { rerender } = render(<CandidateValidationPanel
      probe={{ status: "passed", total_replay_tokens: 12000, token_budget: null }}
      cost={{ status: "passed", replay_te_llm_accounting_complete: true,
        accounting_status: "complete", total_tokens: 2969151, known_total_tokens: 2969151, budget_tokens: null }} />);
    expect(screen.getByText("本次 Agent Replay Token（含主验证与探测）：12000 / 本次 Replay 预算 无上限")).toBeVisible();
    expect(screen.getByText("已知累计 Token（去重）：2969151 / 候选预算 无上限")).toBeVisible();
    expect(screen.getByText("候选累计预算检查：通过（无上限）")).toBeVisible();

    rerender(<CandidateValidationPanel probe={{ total_replay_tokens: null }} cost={{
      status: "inconclusive", reasons: ["total_model_cost_unknown"],
    }} />);
    expect(screen.getByText("本次 Agent Replay Token（含主验证与探测）：未知 / 本次 Replay 预算 未记录")).toBeVisible();
    expect(screen.getByText("已知累计 Token（去重）：未知 / 候选预算 未记录")).toBeVisible();
    expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
  });

  it("separates an unlimited passed budget from incomplete cumulative accounting and Replay blockers", () => {
    render(<CandidateValidationPanel probe={{ status: "inconclusive", reasons: ["aa_completion_unstable"] }} cost={{
      status: "passed", budget_tokens: null, accounting_status: "incomplete", total_tokens: null,
      known_total_tokens: 4273776, agent_replay_tokens: 1816772,
      replay_te_llm_accounting_complete: false,
      te_llm_usage: { known_total_tokens: 2457004, unknown_attempts: 0, complete: false },
      warnings: ["total_model_cost_unknown", "worker_retry_cost_unreconciled", "total_model_cost_unknown"],
      reasons: [],
    }} />);
    expect(screen.getByText("候选累计预算检查：通过（无上限）")).toBeVisible();
    expect(screen.getByText("已知累计 Token（去重）：4273776 / 候选预算 无上限")).toBeVisible();
    expect(screen.getByText("累计总 Token：未知")).toBeVisible();
    expect(screen.getByText("累计统计：记录不完整；总用量未知")).toBeVisible();
    expect(screen.getAllByText("统计提示（不阻断预算检查）：累计用量记录不完整，已知 Token 仅为下界，总用量未知")).toHaveLength(1);
    expect(screen.getByText("统计提示（不阻断预算检查）：历史 Worker 重试用量尚未核对完整")).toBeVisible();
    expect(screen.queryByText(/^成本阻断：/)).not.toBeInTheDocument();
    expect(screen.getByText("探测阻断：A/A 对照的完成结果不稳定")).toBeVisible();
    expect(screen.queryByText("累计统计：完整")).not.toBeInTheDocument();
  });

  it("keeps incomplete finite budgets blocked using the server verdict", () => {
    render(<CandidateValidationPanel cost={{
      status: "inconclusive", budget_tokens: 1000000, accounting_status: "incomplete", total_tokens: null,
      known_total_tokens: 42000, reasons: ["total_model_cost_unknown"], warnings: [],
      replay_te_llm_accounting_complete: true,
    }} />);
    expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
    expect(screen.getByText("成本阻断：累计模型成本不完整，不能确认满足预算")).toBeVisible();
    expect(screen.getByText("累计总 Token：未知")).toBeVisible();
    expect(screen.queryByText(/^统计提示/)).not.toBeInTheDocument();
  });

  it("does not rewrite an old unlimited inconclusive verdict or infer complete totals from legacy coverage", () => {
    const { rerender } = render(<CandidateValidationPanel cost={{
      status: "inconclusive", budget_tokens: null, known_total_tokens: 261119,
      replay_te_llm_accounting_complete: true, reasons: ["total_model_cost_unknown"],
    }} />);
    expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
    expect(screen.getByText("累计统计：完整性未记录；总用量未知")).toBeVisible();
    expect(screen.queryByText("候选累计预算检查：通过（无上限）")).not.toBeInTheDocument();

    rerender(<CandidateValidationPanel cost={{
      status: "passed", budget_tokens: 1000000, known_total_tokens: 261119,
      replay_te_llm_accounting_complete: true,
    }} />);
    expect(screen.getByText("候选累计预算检查：通过")).toBeVisible();
    expect(screen.getByText("累计总 Token：未知")).toBeVisible();
    expect(screen.queryByText("累计统计：完整")).not.toBeInTheDocument();
  });
});
