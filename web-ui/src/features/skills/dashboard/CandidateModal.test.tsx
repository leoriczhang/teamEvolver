// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Candidate } from "@/features/skills/api/client";
import type { EvalResult } from "@/features/replay/api/client";
import CandidateModal from "./CandidateModal";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
vi.mock("@/features/ma/CandidateTargets", () => ({ default: () => null }));
vi.stubGlobal("ResizeObserver", class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

const candidate: Candidate = {
  job_id: "core-candidate", candidate_revision: 4, skill_name: "codingvideo-production-core",
  proposed_action: "update", current_skill_md: "BASELINE CONTENT",
  candidate_skill_md: "CANDIDATE CONTENT",
};
const incomplete: EvalResult = {
  skill_name: "codingvideo-production-core", recommended_publish: false,
  replay: { verdict: "inconclusive", error: "MA Replay branch deadline exceeded" },
};

function show(cand: Candidate, ev: EvalResult, extra = {}) {
  return render(<CandidateModal jobId={cand.job_id} cand={cand} ev={ev} open
    evaluating={false} onClose={vi.fn()} onEvaluate={vi.fn()}
    onFeedbackSaved={vi.fn()} onPublish={vi.fn()} showFeedback={false} {...extra} />);
}

afterEach(cleanup);

describe("CandidateModal feedback placement", () => {
  const props = {
    jobId: "candidate-1", cand: { job_id: "candidate-1", skill_name: "demo" },
    ev: null, evaluating: false, open: true, onClose: vi.fn(),
    onEvaluate: vi.fn(), onFeedbackSaved: vi.fn(),
  };

  it("uses a wide responsive workspace without dialog-level horizontal scrolling", () => {
    render(<CandidateModal {...props} />);
    const dialog = document.querySelector('[data-slot="dialog-content"]');
    expect(dialog?.className).toContain("!max-w-[1440px]");
    expect(dialog?.className).toContain("w-[calc(100vw-24px)]");
    expect(dialog?.className).toContain("overflow-x-hidden");
  });

  it("hides user marks outside Candidate Review", () => {
    render(<CandidateModal {...props} showFeedback={false} />);
    expect(screen.queryByText("用户标记")).toBeNull();
    expect(screen.queryByLabelText("已审阅")).toBeNull();
  });

  it("keeps user marks in Candidate Review by default", () => {
    render(<CandidateModal {...props} />);
    expect(screen.getByText("用户标记")).toBeTruthy();
    expect(screen.getByLabelText("已审阅")).toBeTruthy();
  });
});

describe("candidate detail completeness", () => {
  it("blocks normal publish when a failed replay has no Checklist evidence", () => {
    show(candidate, incomplete);
    expect(screen.getByText("缺少 Checklist 判定，无法确认任务完成；不能按回放发布。")).toBeTruthy();
    expect(screen.queryByText(/发布门禁仅参考三项效率指标/)).toBeNull();
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("回放失败，无法确定")).toBeTruthy();
  });

  it("shows loading instead of absent Skill content for a compact list evaluation", () => {
    show({ ...candidate, current_skill_md: undefined, candidate_skill_md: undefined },
      { ...incomplete, summary_only: true }, { detailLoading: true });
    expect(screen.getByText("正在加载候选详情…")).toBeTruthy();
    expect(screen.queryByText("（无候选内容）")).toBeNull();
    expect(screen.queryByText(/当前技能库未找到对应/)).toBeNull();
    expect(screen.queryByText("本次实时评估")).toBeNull();
  });

  it("shows a detail load failure explicitly without displaying empty content as fact", () => {
    show({ ...candidate, current_skill_md: undefined, candidate_skill_md: undefined },
      { ...incomplete, summary_only: true }, { detailError: "详情请求失败" });
    expect(screen.getByRole("alert").textContent).toContain("详情请求失败");
    expect(screen.queryByText("（无候选内容）")).toBeNull();
  });

  it("permits normal publish only after both Checklist sides and replay passed", () => {
    const report = { all_satisfied: true, total: 1, satisfied_count: 1,
      items: [{ id: "1", text: "交付视频", satisfied: true, evidence: "真实产物已核对" }] };
    show(candidate, {
      recommended_publish: true,
      replay: { verdict: "accept", checklist: { baseline: report, candidate: report },
        decision_policy: { verdict: "accept", decision_basis: "interaction_turns_decreased" } },
    });
    expect((screen.getByRole("button", { name: "按回放发布" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getAllByText("BASELINE CONTENT").length).toBeGreaterThan(0);
  });

  it("requests full replay only on demand and preserves unavailable metrics", async () => {
    mocks.api.mockResolvedValue({ ...candidate, evaluation: {
      replay: { verdict: "inconclusive", cases: [{ baseline: { response: "BASELINE OUTPUT", tool_call_count: 0 },
        candidate: { response: "CANDIDATE OUTPUT" } }] },
    } });
    show(candidate, { ...incomplete, replay_details_omitted: true });
    expect(mocks.api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "加载完整回放证据" }));
    expect(await screen.findByText("CANDIDATE OUTPUT")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledWith("/api/skill-candidates/core-candidate/detail");
    expect(screen.getByText("工具 0")).toBeTruthy();
    expect(screen.getByText("工具 不可用")).toBeTruthy();
    expect(screen.getAllByText("Token 不可用")).toHaveLength(2);
  });

  it("shows unavailable Checklist judgements separately from unmet items", () => {
    show(candidate, { ...incomplete, replay: { verdict: "inconclusive", checklist: { candidate: {
      total: 1, satisfied_count: 0, all_satisfied: false, judge: "unavailable",
      items: [{ id: "video", text: "交付视频" }],
    } } } });
    expect(screen.getByText("判定不完整")).toBeTruthy();
    expect(screen.getByText("未判定")).toBeTruthy();
    expect(screen.queryByText("未满足 1 项")).toBeNull();
  });
});
