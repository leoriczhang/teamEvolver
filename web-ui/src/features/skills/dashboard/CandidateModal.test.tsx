// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  it("shows the frozen generation version next to its baseline contents", () => {
    show({ ...candidate, base_version: 12, baseline: { kind: "version", version: 12 } }, incomplete);
    expect(screen.getByText("基于 Skill v12")).toBeTruthy();
    expect(screen.getByText("基线 SKILL.md · 基于 Skill v12")).toBeTruthy();
    expect(screen.getByText("BASELINE CONTENT", { selector: "pre" })).toBeTruthy();
  });

  it("distinguishes a new skill from a legacy candidate with no recorded base", () => {
    const view = show({ ...candidate, base_version: 0, proposed_action: "create", current_skill_md: "" }, incomplete);
    expect(screen.getByText("新建技能 · 无基线版本")).toBeTruthy();
    view.unmount();
    show({ ...candidate, current_skill_md: "" }, incomplete);
    expect(screen.getByText("基线版本未记录")).toBeTruthy();
  });

  it("shows publishing progress and keeps a failed publication visible", async () => {
    let reject!: (reason: Error) => void;
    const onPublish = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
    show(candidate, incomplete, { onPublish });
    fireEvent.click(screen.getByRole("button", { name: "强制发布" }));
    expect((screen.getByRole("button", { name: "发布中…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(onPublish).toHaveBeenCalledWith("force", 4);
    reject(new Error("执行验证门禁未通过"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("执行验证门禁未通过"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect((screen.getByRole("button", { name: "强制发布" }) as HTMLButtonElement).disabled).toBe(false);
    expect(onPublish).toHaveBeenCalledTimes(1);
  });
  it("shows recorded models and environments alongside business and ingest times", () => {
    show({ ...candidate, execution_provenance: [{
      session_id: "source-ma-session", business_time: "2026-11-20T08:46:50Z",
      ingested_at: "2026-11-20T09:20:26Z",
      agents: [{ id: "source-agent", version: 30, model: { id: "gpt-6.2-sol" } }],
      environment: { id: "source-environment", name: "ecommerce-cloud-recovery" },
    }] }, incomplete);
    fireEvent.click(screen.getByText("进化来源 · 1 份执行快照"));
    expect(screen.getByText(/模型：gpt-6.2-sol/)).toBeTruthy();
    expect(screen.getByText(/ecommerce-cloud-recovery/)).toBeTruthy();
    expect(screen.getByText(/业务时间：2026-11-20T08:46:50Z/).textContent)
      .toContain("入库时间：2026-11-20T09:20:26Z");
  });

  it("allows supported experience review without requiring MA or A/B Checklist", () => {
    show(candidate, {
      status: "evaluated", recommended_publish: true,
      replay: { verdict: "accept", validation_method: "experience_review",
        runtime_gate: { status: "not_required" }, validation_scope: "权限缺失处理",
        assessment: { reason: "引用实际依赖错误，补充暂停条件", evidence_refs: ["source-session"],
          scope: "权限缺失处理", verdict: "accept", source: "model" }, ab_verdict: "not_run" },
    });
    expect((screen.getByRole("button", { name: "按验证结论发布" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("经验审阅不要求创建业务 Session。")).toBeTruthy();
    expect(screen.queryByText(/缺少 Checklist 判定/)).toBeNull();
  });

  it("separates one completed Candidate scope from an undetermined A/B comparison", () => {
    const onEvaluate = vi.fn();
    show(candidate, {
      status: "evaluated", recommended_publish: true,
      replay: { verdict: "accept", validation_method: "candidate_execution",
        runtime_gate: { status: "passed" }, validation_scope: "分镜阶段",
        error: "旧 A/B Baseline 超时",
        basic_validation: { status: "passed" }, candidate_completed: 1, ab_completed: 0,
        ab_verdict: "inconclusive",
        checklist: { candidate: { all_satisfied: true, total: 1, satisfied_count: 1,
          judge: "model", items: [{ id: "R1", text: "完成分镜", satisfied: true, evidence: "已归档分镜" }] } } },
    }, { onEvaluate });
    expect((screen.getByRole("button", { name: "按验证结论发布" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("基础验证通过")).toBeTruthy();
    expect(screen.getByText(/候选已完成 1 个场景/).textContent).toContain("未判定");
    fireEvent.click(screen.getByRole("button", { name: "完整 A/B" }));
    expect(onEvaluate).toHaveBeenCalledWith(true, "full_ab");
  });

  it("shows the active replay progress instead of a cached blocked result after reopening", () => {
    show({ ...candidate, review_status: "validating",
      replay_progress: { total: 5, completed: 2, succeeded: 1, failed: 1 } },
    { ...incomplete, status: "blocked", replay: {
      reason: "Discover the configured Replay Environment before validation",
    } });
    expect(screen.getByRole("status").textContent).toContain("已完成 2 / 5");
    expect(screen.queryByText(/评估受阻/)).toBeNull();
    expect(screen.queryByText(/Discover the configured/)).toBeNull();
    expect((screen.getByRole("button", { name: "评估中…" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "强制发布" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows pending publication after reopening without claiming to evaluate cases", () => {
    show({ ...candidate, review_status: "validating", publication_pending: true,
      replay_progress: { total: 2, completed: 0, succeeded: 0, failed: 0 } }, incomplete);
    expect(screen.getByRole("status").textContent).toContain("正在发布候选");
    expect(screen.queryByText(/已完成 0 \/ 2/)).toBeNull();
    expect(screen.queryByText("正在评估")).toBeNull();
    expect((screen.getByRole("button", { name: "发布中…" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "完整 A/B" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("blocks normal publish when a failed replay has no Checklist evidence", () => {
    show(candidate, incomplete);
    expect(screen.getByText("缺少 Checklist 判定，无法确认任务完成；不能按回放发布。")).toBeTruthy();
    expect(screen.queryByText(/发布门禁仅参考三项效率指标/)).toBeNull();
    expect((screen.getByRole("button", { name: "按验证结论发布" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("回放失败，无法确定")).toBeTruthy();
  });

  it("keeps branch failure evidence visible alongside the aggregate replay error", () => {
    show(candidate, { ...incomplete, replay: {
      verdict: "inconclusive", error: "MCP isolation not configured",
      cases: [{
        baseline: { error: "Runtime Bundle contains no files for configured paths" },
        candidate: { error: "MA HTTP 400: InvalidParameter" },
      }],
    } });
    expect(screen.getByText("回放失败：MCP isolation not configured")).toBeTruthy();
    expect(screen.getByText("Runtime Bundle contains no files for configured paths")).toBeTruthy();
    expect(screen.getByText("MA HTTP 400: InvalidParameter")).toBeTruthy();
    expect((screen.getByRole("button", { name: "按验证结论发布" }) as HTMLButtonElement).disabled).toBe(true);
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
    expect((screen.getByRole("button", { name: "按验证结论发布" }) as HTMLButtonElement).disabled).toBe(false);
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
