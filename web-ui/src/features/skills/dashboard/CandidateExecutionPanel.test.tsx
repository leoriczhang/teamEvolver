// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import type { Candidate, ExecutionComponent } from "@/features/skills/api/client";
import CandidateExecutionPanel from "./CandidateExecutionPanel";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const part = (component_id: string, content: string, kind: ExecutionComponent["kind"]): ExecutionComponent => ({
  component_id, content, kind, slot: kind === "agent_prompt" ? "system" : "context", order: 0,
  content_hash: `hash-${content}`, revision: 1,
});
const candidate: Candidate = {
  job_id: "candidate/business", skill_name: "report", candidate_revision: 3, review_status: "open",
  allowed_modules: ["prompt"], actual_modules: ["prompt"], execution_profile_id: "business-profile",
  base_release_id: "release-old", session_ids: ["source-session-one", "source-session-two"],
  current_case_ids: ["case-one", "case-two"],
  baseline_execution_plan: { components: [
    part("agent-prompt", "Original instructions", "agent_prompt"),
    part("business-context", "Original context", "context_fragment"),
  ] },
  candidate_execution_plan: { components: [
    part("agent-prompt", "Proposed instructions", "agent_prompt"),
    part("business-context", "Proposed context", "context_fragment"),
  ] },
  component_changes: [
    { component_id: "agent-prompt", content: "Proposed instructions", reason: "Original prompt reason", evidence_refs: ["source-session-one"] },
    { component_id: "business-context", content: "Proposed context", reason: "Original context reason", evidence_refs: ["source-session-two"] },
  ],
  publication_gate: { passed: false, reasons: ["execution_not_observed", "regression_coverage_missing"] },
};

describe("CandidateExecutionPanel", () => {
  it.each(["outer", "replay"] as const)("shows cumulative candidate cost from the %s evaluation gate without mixing current Replay cost", location => {
    const incomplete = location === "replay";
    const cost_gate = {
      status: incomplete ? "inconclusive" : "passed", reasons: incomplete ? ["total_model_cost_unknown"] : [],
      replay_te_llm_accounting_complete: !incomplete,
      known_total_tokens: 41000, budget_tokens: 100000,
      agent_replay_tokens: incomplete ? null : 30000,
      te_llm_usage: { known_total_tokens: 11000, unknown_attempts: incomplete ? 2 : 0 },
      prior_validation_runs: 2,
    };
    const evaluation = { replay: {
      probe_gate: { status: "passed", total_replay_tokens: 7000, token_budget: 100000 },
      ...(location === "replay" ? { cost_gate } : {}),
    }, ...(location === "outer" ? { cost_gate } : {}) };
    render(<CandidateExecutionPanel candidate={{ ...candidate, evaluation: evaluation as unknown as Candidate["evaluation"] }}
      readOnly onUpdated={vi.fn()} />);
    expect(screen.getByText("本次 Agent Replay Token（含主验证与探测）：7000 / 本次 Replay 预算 100000")).toBeVisible();
    expect(screen.getByText("已知累计 Token（去重）：41000 / 候选预算 100000")).toBeVisible();
    expect(screen.getByText("纳入的此前验证：2 次")).toBeVisible();
    expect(screen.getByText(/包含候选生成、裁判、已执行探测及此前各次验证/)).toBeVisible();
    if (incomplete) {
      expect(screen.getByText("候选累计预算检查：证据不足／阻断")).toBeVisible();
      expect(screen.getByText("累计 Agent Replay Token：未知")).toBeVisible();
      expect(screen.getByText("累计 TE 模型 Token：11000 / 已记录但用量未知的调用：2")).toBeVisible();
      expect(screen.getByText("成本阻断：累计模型成本不完整，不能确认满足预算")).toBeVisible();
    } else {
      expect(screen.getByText("候选累计预算检查：通过")).toBeVisible();
      expect(screen.getByText("累计 Agent Replay Token：30000")).toBeVisible();
      expect(screen.getByText("累计 TE 模型 Token：11000 / 已记录但用量未知的调用：0")).toBeVisible();
    }
  });

  it("shows actual baseline/candidate text, fixed composition and publication reasons", () => {
    render(<CandidateExecutionPanel candidate={candidate} readOnly onUpdated={vi.fn()} />);
    expect(screen.getByText("Original instructions")).toBeVisible();
    expect(screen.getByText("Proposed instructions")).toBeVisible();
    expect(screen.getByText("Original context")).toBeVisible();
    expect(screen.getByText("Proposed context")).toBeVisible();
    expect(screen.getByText("冻结基线发布：release-old")).toBeVisible();
    expect(screen.getByText("尚未核验实际执行内容与候选组合一致。")).toBeVisible();
    expect(screen.getByText("共享资产影响的 Skill 尚未全部覆盖回归。")).toBeVisible();
    expect(screen.queryByRole("button", { name: /编辑/ })).not.toBeInTheDocument();
  });

  it("edits only component text with original evidence, preserves sibling edits and sends the revision", async () => {
    const updated = { ...candidate, candidate_revision: 4 };
    vi.mocked(api).mockResolvedValue(updated);
    const onUpdated = vi.fn();
    render(<CandidateExecutionPanel candidate={candidate} readOnly={false} onUpdated={onUpdated} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 agent-prompt" }));
    fireEvent.change(screen.getByLabelText("候选业务文本"), { target: { value: "Refined instructions" } });
    fireEvent.change(screen.getByLabelText("业务修改原因"), { target: { value: "Clarify repeated requirement" } });
    fireEvent.click(screen.getByRole("button", { name: "保存业务候选" }));
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(updated));
    const [path, options] = vi.mocked(api).mock.calls[0];
    expect(path).toBe("/api/skill-candidates/candidate%2Fbusiness/execution-content");
    expect(JSON.parse(options?.body as string)).toEqual({
      candidate_revision: 3,
      component_changes: [candidate.component_changes![1], {
        component_id: "agent-prompt", content: "Refined instructions", reason: "Clarify repeated requirement",
        evidence_refs: ["source-session-one"],
      }],
    });
  });

  it("reviews a variable group and saves one exact member without changing sibling hosts or values", async () => {
    const member = (id: string, host: string): ExecutionComponent => ({
      ...part(id, `Rule ${id}`, "context_fragment"),
      binding: { inline_variable: { component_id: host, name: "rules", source_value_hash: `source-${id}` } },
    });
    const components = [member("value-one", "host-one"), member("value-two", "host-two")];
    const scoped: Candidate = { ...candidate, component_changes: [],
      baseline_execution_plan: { binding: { profile_id: "business-profile" }, components },
      candidate_execution_plan: { binding: { profile_id: "business-profile" }, components },
    };
    vi.mocked(api).mockResolvedValue({ ...scoped, candidate_revision: 4 });
    render(<CandidateExecutionPanel candidate={scoped} readOnly={false} onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByText("变量组 rules · 2 个取值 · 2 个注入成员"));
    expect(screen.getByText("注入位置：host-one 中的 {{rules}}")).toBeVisible();
    expect(screen.getByText("注入位置：host-two 中的 {{rules}}")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "编辑 value-one" }));
    fireEvent.change(screen.getByLabelText("候选业务文本"), { target: { value: "Clarified first value" } });
    fireEvent.change(screen.getByLabelText("业务修改原因"), { target: { value: "Original source correction" } });
    fireEvent.click(screen.getByRole("button", { name: "保存业务候选" }));
    await waitFor(() => expect(api).toHaveBeenCalledOnce());
    expect(JSON.parse(vi.mocked(api).mock.calls[0][1]?.body as string)).toEqual({
      candidate_revision: 3, component_changes: [{ component_id: "value-one", content: "Clarified first value",
        reason: "Original source correction", evidence_refs: ["source-session-one", "source-session-two"] }],
    });
    expect(components[1].content).toBe("Rule value-two");
  });

  it("limits editing to the frozen component allowlist and keeps the host template read-only", async () => {
    const scoped: Candidate = {
      ...candidate,
      baseline_execution_plan: { ...candidate.baseline_execution_plan!, environment: {
        evolution: { editable_component_ids: ["business-context"] },
      } },
      candidate_execution_plan: { ...candidate.candidate_execution_plan!, environment: {
        evolution: { editable_component_ids: ["business-context"] },
      } },
      component_changes: [candidate.component_changes![1]],
    };
    vi.mocked(api).mockResolvedValue({ ...scoped, candidate_revision: 4 });
    const view = render(<CandidateExecutionPanel candidate={scoped} readOnly={false} onUpdated={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "编辑 agent-prompt" })).not.toBeInTheDocument();
    expect(screen.getByText("本轮只读")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "编辑 business-context" }));
    fireEvent.change(screen.getByLabelText("候选业务文本"), { target: { value: "Improved context" } });
    fireEvent.click(screen.getByRole("button", { name: "保存业务候选" }));
    await waitFor(() => expect(api).toHaveBeenCalledOnce());
    expect(JSON.parse(vi.mocked(api).mock.calls[0][1]?.body as string).component_changes).toEqual([{
      ...candidate.component_changes![1], content: "Improved context",
    }]);
    await waitFor(() => expect(screen.queryByLabelText("候选业务文本")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "编辑 business-context" }));
    view.rerender(<CandidateExecutionPanel candidate={{ ...scoped, baseline_execution_plan: {
      ...scoped.baseline_execution_plan!, environment: { evolution: { editable_component_ids: ["agent-prompt"] } },
    } }} readOnly={false} onUpdated={vi.fn()} />);
    expect(screen.queryByLabelText("候选业务文本")).not.toBeInTheDocument();
    expect(api).toHaveBeenCalledOnce();
  });

  it("preserves legacy editing for every component when no component allowlist was configured", () => {
    render(<CandidateExecutionPanel candidate={candidate} readOnly={false} onUpdated={vi.fn()} />);
    expect(screen.getByRole("button", { name: "编辑 agent-prompt" })).toHaveTextContent(/^编辑$/);
    expect(screen.getByRole("button", { name: "编辑 business-context" })).toHaveTextContent(/^编辑$/);
    expect(screen.queryByText("本轮只读")).not.toBeInTheDocument();
  });

  it("does not allow edits without Prompt scope and closes editing when validation starts", async () => {
    const view = render(<CandidateExecutionPanel candidate={{ ...candidate, allowed_modules: ["skill"] }}
      readOnly={false} onUpdated={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /编辑 agent-prompt/ })).not.toBeInTheDocument();
    view.rerender(<CandidateExecutionPanel candidate={candidate} readOnly={false} onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 agent-prompt" }));
    expect(screen.getByLabelText("候选业务文本")).toBeVisible();
    view.rerender(<CandidateExecutionPanel candidate={{ ...candidate, review_status: "validating" }}
      readOnly={false} onUpdated={vi.fn()} />);
    await waitFor(() => expect(screen.queryByLabelText("候选业务文本")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /编辑 agent-prompt/ })).not.toBeInTheDocument();
    expect(screen.queryByText("登记外部复核声明")).not.toBeInTheDocument();
  });

  it("attaches a reviewer statement covering every current Case without claiming automatic truth", async () => {
    vi.mocked(api).mockResolvedValue(candidate);
    const onUpdated = vi.fn();
    render(<CandidateExecutionPanel candidate={candidate} readOnly={false} onUpdated={onUpdated} />);
    expect(screen.getByText(/并不据此证明 LLM 已自动判定业务事实正确/)).toBeVisible();
    fireEvent.click(screen.getByText("登记外部复核声明"));
    fireEvent.change(screen.getByLabelText("外部复核结论"), { target: { value: "passed" } });
    fireEvent.change(screen.getByLabelText("外部复核方法"), { target: { value: "programmatic" } });
    fireEvent.change(screen.getByLabelText("外部证据引用"), { target: { value: "report:independent-check-42" } });
    fireEvent.change(screen.getByLabelText("外部复核说明"), { target: { value: "Checked output against source fixture" } });
    fireEvent.click(screen.getByRole("button", { name: "提交外部复核声明" }));
    await waitFor(() => expect(onUpdated).toHaveBeenCalled());
    const [path, options] = vi.mocked(api).mock.calls[0];
    expect(path).toBe("/api/skill-candidates/candidate%2Fbusiness/independent-validation");
    expect(JSON.parse(options?.body as string)).toEqual({
      candidate_revision: 3, status: "passed", method: "programmatic", evidence_ref: "report:independent-check-42",
      case_ids: ["case-one", "case-two"], notes: "Checked output against source fixture",
    });
  });

  it("cannot submit an independent statement when current Case identifiers are unavailable", () => {
    render(<CandidateExecutionPanel candidate={{ ...candidate, current_case_ids: [] }} readOnly={false} onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByText("登记外部复核声明"));
    fireEvent.change(screen.getByLabelText("外部证据引用"), { target: { value: "report:one" } });
    expect(screen.getByRole("button", { name: "提交外部复核声明" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("尚无可核验的当前用例 ID");
  });

  it("displays an expired reviewer statement without treating it as current verification", () => {
    render(<CandidateExecutionPanel candidate={{ ...candidate, independent_validation: {
      candidate_revision: 2, status: "passed", method: "domain_review", evidence_ref: "review:older",
      case_ids: ["case-one", "case-two"], notes: "Reviewed previous version", reviewer: "reviewer-one",
    } }} readOnly onUpdated={vi.fn()} />);
    expect(screen.getByText("独立验证状态：通过（版本已过期）")).toBeVisible();
  });
});
