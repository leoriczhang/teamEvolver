// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CandidateReviewView from "./CandidateReviewView";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api, tenantHeaders: () => new Headers() }));
vi.mock("@/features/ma/CandidateTargets", () => ({ default: () => null }));
vi.stubGlobal("ResizeObserver", class {
  observe() {}
  unobserve() {}
  disconnect() {}
});
vi.mock("@/features/skills/dashboard/CandidateFeedback", () => ({
  default: ({ readOnly }: { readOnly: boolean }) => <span data-testid="feedback-mode">{String(readOnly)}</span>,
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("candidate replay state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps review details open when publication does not write a version", async () => {
    const candidate = {
      job_id: "candidate", candidate_revision: 4, skill_name: "demo", review_status: "open",
      candidate_skill_md: "CANDIDATE", current_skill_md: "CURRENT",
      evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
    };
    mocks.api.mockImplementation(async (path: string) => {
      if (path.endsWith("/detail?include_replay=false")) return candidate;
      if (path === "/api/skill-candidates/candidate/validate") {
        return { job_id: "publish-check", status_url: "/api/jobs/publish-check" };
      }
      if (path === "/api/jobs/publish-check") return { status: "succeeded" };
      if (path === "/api/skill-evolution/jobs/publish-check/result") return {
        result: { status: "inconclusive", evaluation: { replay: { reason: "执行门禁不满足" } } },
      };
      return { candidates: [candidate] };
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<CandidateReviewView active canManage />);
    fireEvent.click(await screen.findByRole("button", { name: "查看回放并发布" }));
    fireEvent.click(await screen.findByRole("button", { name: "强制发布" }));
    expect((await screen.findByRole("alert")).textContent).toContain("执行门禁不满足");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledWith("/api/skill-candidates/candidate/validate", expect.objectContaining({
      body: JSON.stringify({ mode: "force", candidate_revision: 4 }),
    }));
  });

  it("shows active server progress even when an older evaluation is cached", async () => {
    mocks.api.mockResolvedValue({
      candidates: [{
        job_id: "candidate", skill_name: "demo", review_status: "validating",
        replay_progress: { total: 6, completed: 2, succeeded: 2, failed: 0 },
        evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
      }],
    });
    const view = render(<CandidateReviewView active />);
    expect(await screen.findByText("评估中 2/6")).toBeTruthy();
    expect(mocks.api).toHaveBeenCalledWith("/api/skill-candidates?compact=true&scope=open");
    expect(screen.queryByText("已完成")).toBeNull();
    expect(screen.queryByText("持平 / 有增加")).toBeNull();
    view.unmount();
  });

  it("distinguishes an inconclusive evaluation from efficiency regression", async () => {
    mocks.api.mockResolvedValue({
      candidates: [{
        job_id: "candidate", skill_name: "demo", review_status: "open",
        evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
      }],
    });
    const view = render(<CandidateReviewView active />);
    await waitFor(() => expect(screen.getByText("无法确定")).toBeTruthy());
    expect(screen.queryByText("持平 / 有增加")).toBeNull();
    view.unmount();
  });

  it("labels a queued force publication as publishing instead of evaluating", async () => {
    mocks.api.mockResolvedValue({ candidates: [{
      job_id: "candidate", skill_name: "demo", review_status: "validating", publication_pending: true,
      evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
    }] });
    render(<CandidateReviewView active />);
    expect(await screen.findByText("发布中…")).toBeTruthy();
    expect(screen.queryByText(/评估中/)).toBeNull();
  });

  it("keeps open candidates actionable when the API returns an empty decision object", async () => {
    mocks.api.mockResolvedValue({
      candidates: [{
        job_id: "candidate", skill_name: "demo", review_status: "open", decision: {},
        evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
      }],
    });
    const view = render(<CandidateReviewView active canManage />);
    await screen.findByText("demo");
    expect(screen.getByTestId("feedback-mode").textContent).toBe("false");
    expect(screen.getByRole("button", { name: "重新评估" })).toBeTruthy();
    view.unmount();
  });

  it("shows blocked acceptance without inventing an efficiency conclusion", async () => {
    mocks.api.mockResolvedValue({
      candidates: [{
        job_id: "candidate", skill_name: "demo", review_status: "open",
        evaluation: { status: "blocked", recommended_publish: false,
          replay: { status: "blocked", reason: "历史输入附件缺失" } },
      }],
    });
    const view = render(<CandidateReviewView active canManage />);
    expect(await screen.findByText("评估受阻")).toBeTruthy();
    expect(screen.getByText("历史输入附件缺失")).toBeTruthy();
    expect(screen.getByText("无法确定")).toBeTruthy();
    expect(screen.queryByText("评估结束")).toBeNull();
    expect(screen.queryByText("持平 / 有增加")).toBeNull();
    expect(mocks.api.mock.calls.every(([path]) => path === "/api/skill-candidates?compact=true&scope=open")).toBe(true);
    view.unmount();
  });

  it("loads actual detail on click without claiming that compact content is absent", async () => {
    let resolveDetail!: (detail: any) => void;
    const pending = new Promise(resolve => { resolveDetail = resolve; });
    mocks.api.mockImplementation((path: string) => path.includes("/detail")
      ? pending : Promise.resolve({ candidates: [{
        job_id: "candidate", candidate_revision: 4, skill_name: "demo",
        evaluation: { summary_only: true, replay: { verdict: "inconclusive" } },
      }] }));
    render(<CandidateReviewView active />);
    fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(screen.getByText("正在加载候选详情…")).toBeTruthy();
    expect(screen.queryByText("（无候选内容）")).toBeNull();
    expect(mocks.api).toHaveBeenCalledWith("/api/skill-candidates/candidate/detail?include_replay=false");
    resolveDetail({ job_id: "candidate", candidate_revision: 4, skill_name: "demo",
      current_skill_md: "CURRENT REAL SKILL", candidate_skill_md: "CANDIDATE REAL SKILL",
      evaluation: { replay_details_omitted: true, replay: { verdict: "inconclusive" } } });
    expect((await screen.findAllByText("CURRENT REAL SKILL")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "加载完整回放证据" })).toBeTruthy();
    expect(screen.queryByText("正在加载候选详情…")).toBeNull();
  });

  it("exposes detail request errors and allows a retry", async () => {
    mocks.api.mockImplementation((path: string) => path.includes("/detail")
      ? Promise.reject(new Error("网络中断")) : Promise.resolve({ candidates: [{
        job_id: "candidate", skill_name: "demo",
        evaluation: { summary_only: true, replay: { verdict: "inconclusive" } },
      }] }));
    render(<CandidateReviewView active />);
    fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
    expect((await screen.findByRole("alert")).textContent).toContain("网络中断");
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
    expect(screen.queryByText("（无候选内容）")).toBeNull();
  });
});
