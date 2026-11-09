// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import CandidateModal from "./CandidateModal";
import type { Candidate } from "@/features/skills/api/client";

// Dialog focus may open a Radix tooltip; jsdom has no native ResizeObserver.
beforeAll(() => vi.stubGlobal("ResizeObserver", class {
  observe() {}
  unobserve() {}
  disconnect() {}
}));
afterAll(() => vi.unstubAllGlobals());

const props = {
  jobId: "candidate-1",
  cand: { job_id: "candidate-1", skill_name: "demo" },
  ev: null,
  evaluating: false,
  open: true,
  onClose: vi.fn(),
  onEvaluate: vi.fn(),
  onFeedbackSaved: vi.fn(),
};

describe("CandidateModal feedback placement", () => {
  it("shows external scores as the chosen criterion and keeps efficiency and Checklist as records", () => {
    const view = render(<CandidateModal {...props} ev={{ replay: {
      verdict: "accept", decision_policy: { policy: "external-agent-score", verdict: "accept", decision_basis: "external_score_improved" },
      external_evaluation: { baseline_score: 65, candidate_score: 85, score_delta: 20, case_count: 1,
        evaluated_case_count: 1, min_score: 70, min_improvement: 5, decision_basis: "external_score_improved" },
      efficiency: { dimensions: { total_tokens: { baseline: 100, candidate: 200, delta: -100, reduction_ratio: -1, winner: "baseline" } } },
    } }} showFeedback={false} />);
    expect(screen.getByText("True Replay 外部评分结论")).toBeTruthy();
    expect(screen.getByRole("table", { name: "A/B 外部评分对比" })).toBeTruthy();
    expect(screen.getByText("✅ Checklist 执行记录")).toBeTruthy();
    expect(screen.getByRole("table", { name: "A/B 效率对比" })).toBeTruthy();
    expect(screen.queryByText("三项持平 / 数据不完整")).toBeNull();
    view.unmount();
  });

  it("uses the frozen standard policy without demanding an independent review statement", () => {
    const onPublish = vi.fn();
    const candidate: Candidate = {
      job_id: "standard-business", candidate_kind: "execution", review_status: "open",
      candidate_revision: 3, allowed_modules: ["prompt"],
      baseline_execution_plan: { components: [] }, candidate_execution_plan: { components: [] },
      validation_plan: { validation_strategy: "standard-v1" },
    };
    const view = render(<CandidateModal {...props} jobId={candidate.job_id} cand={candidate}
      ev={{ recommended_publish: true }} onPublish={onPublish} />);
    const publish = screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement;
    expect(publish.disabled).toBe(false);
    expect(screen.queryByRole("region", { name: "独立外部复核" })).toBeNull();
    fireEvent.click(publish);
    expect(onPublish).toHaveBeenCalledWith("auto");
    view.rerender(<CandidateModal {...props} jobId={candidate.job_id}
      cand={{ ...candidate, validation_plan: {} }} ev={{ recommended_publish: true }} onPublish={onPublish} />);
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(true);
    view.unmount();
  });

  it("reviews a zero-Skill execution candidate without constructing or editing a Skill document", () => {
    const component = {
      component_id: "pure:prompt", kind: "agent_prompt" as const, slot: "system" as const,
      order: 0, content: "Actual business prompt", content_hash: "hash-one", revision: 1,
    };
    const candidate: Candidate = {
      job_id: "execution-candidate", candidate_kind: "execution", execution_profile_id: "pure-business-profile",
      skill_name: null, candidate_skill_name: null, candidate_skill: null, current_skill: null,
      review_status: "open", candidate_revision: 2, allowed_modules: ["skill", "prompt"], actual_modules: ["prompt"],
      skill_dependencies: [], skill_changed: false,
      baseline_execution_plan: { components: [component] },
      candidate_execution_plan: { components: [{ ...component, content: "Refined business prompt", revision: 2 }] },
    };
    const view = render(<CandidateModal {...props} jobId={candidate.job_id} cand={candidate}
      ev={{ recommended_publish: false, current_skill_md: "SHOULD NOT DISPLAY BASELINE SKILL",
        candidate_skill_md: "SHOULD NOT DISPLAY CANDIDATE SKILL" }} showFeedback={false}
      canManageExecution onPublish={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "评估详情 · pure-business-profile" })).toBeTruthy();
    expect(screen.getByText("Prompt-only · 纯业务 Prompt／上下文候选")).toBeTruthy();
    expect(screen.getByText("Skill 依赖：0（不适用 Skill 版本）")).toBeTruthy();
    expect(screen.getByRole("button", { name: "编辑 pure:prompt" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    expect(screen.queryByText("技能包变更")).toBeNull();
    expect(screen.queryByText(/当前 SKILL.md|候选 SKILL.md|SHOULD NOT DISPLAY/)).toBeNull();
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(true);
    view.unmount();
  });

  it("uses a wide responsive workspace without dialog-level horizontal scrolling", () => {
    const view = render(<CandidateModal {...props} />);
    const dialog = document.querySelector('[data-slot="dialog-content"]');
    expect(dialog?.className).toContain("!max-w-[1440px]");
    expect(dialog?.className).toContain("w-[calc(100vw-24px)]");
    expect(dialog?.className).toContain("overflow-x-hidden");
    view.unmount();
  });

  it("hides user marks outside Candidate Review", () => {
    const view = render(<CandidateModal {...props} showFeedback={false} />);
    expect(screen.queryByText("用户标记")).toBeNull();
    expect(screen.queryByLabelText("已审阅")).toBeNull();
    view.unmount();
  });

  it("keeps user marks in Candidate Review by default", () => {
    const view = render(<CandidateModal {...props} />);
    expect(screen.getByText("用户标记")).toBeTruthy();
    expect(screen.getByLabelText("已审阅")).toBeTruthy();
    view.unmount();
  });

  it("shows the business composition before evaluation and hides Skill editing in Prompt-only scope", () => {
    const component = {
      component_id: "prompt", kind: "agent_prompt" as const, slot: "system" as const,
      order: 0, content: "Current business prompt", content_hash: "hash", revision: 1,
    };
    const candidate: Candidate = {
      ...props.cand, review_status: "open", candidate_revision: 3, allowed_modules: ["prompt"],
      baseline_execution_plan: { components: [component] },
      candidate_execution_plan: { components: [component] },
    };
    const view = render(<CandidateModal {...props} cand={candidate} showFeedback={false} />);
    expect(screen.getByText("业务 Prompt／上下文变更与组合")).toBeTruthy();
    expect(screen.getByText("Current business prompt")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    expect(screen.queryByRole("button", { name: "编辑 prompt" })).toBeNull();
    expect(screen.queryByText("登记外部复核声明")).toBeNull();
    view.unmount();
  });

  it("blocks composition publication until the current revision has an external review statement", () => {
    const candidate: Candidate = {
      ...props.cand, review_status: "open", candidate_revision: 3, allowed_modules: ["prompt"],
      baseline_execution_plan: { components: [] }, candidate_execution_plan: { components: [] },
    };
    const view = render(<CandidateModal {...props} cand={candidate} ev={{ recommended_publish: false }}
      showFeedback={false} onPublish={vi.fn()} />);
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "强制发布" }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<CandidateModal {...props} cand={{ ...candidate, independent_validation: {
      candidate_revision: 3, status: "passed", method: "domain_review", evidence_ref: "review:one",
      case_ids: ["case-one"], notes: "Reviewed independently",
    } }} ev={{ recommended_publish: false }} showFeedback={false} onPublish={vi.fn()} />);
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(false);
    view.unmount();
  });
});
