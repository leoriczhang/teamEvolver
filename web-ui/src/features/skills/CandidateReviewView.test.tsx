// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CandidateReviewView from "./CandidateReviewView";

const mocks = vi.hoisted(() => ({ api: vi.fn(), modal: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
vi.mock("@/features/skills/dashboard/CandidateModal", () => ({ default: (props: unknown) => { mocks.modal(props); return null; } }));
vi.mock("@/features/skills/dashboard/CandidateFeedback", () => ({ default: () => null }));

describe("candidate replay state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("names a pure Prompt candidate by execution profile without a fake Skill or file-change badge", async () => {
    mocks.api.mockResolvedValue({ candidates: [{
      job_id: "prompt-candidate", candidate_kind: "execution", execution_profile_id: "business-profile",
      skill_name: null, candidate_skill_name: null, review_status: "open", skill_dependencies: [],
      bundle_diff: { changed_count: 1, files: [{ path: "SKILL.md", status: "modified" }] },
      evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
    }] });
    const view = render(<CandidateReviewView active />);
    expect(await screen.findByText("business-profile")).toBeTruthy();
    expect(screen.getByText("Prompt-only · 0 Skill 依赖")).toBeTruthy();
    expect(screen.queryByText("变更 1 个文件")).toBeNull();
    view.unmount();
  });

  it("keeps an open candidate with the native empty decision object actionable", async () => {
    const candidate = {
      job_id: "open-prompt", candidate_kind: "execution", execution_profile_id: "context-profile",
      review_status: "open", decision: {},
      evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
    };
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/detail") ? candidate : { candidates: [candidate] });
    const view = render(<CandidateReviewView active canManage />);
    expect(await screen.findByRole("button", { name: "重新评估" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "驳回" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看回放并发布" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/api/skill-candidates/open-prompt/detail"));
    expect(mocks.modal.mock.lastCall?.[0]).toMatchObject({ open: true, readOnly: false, cand: candidate });
    view.unmount();
  });

  it.each([
    { review_status: "open", decision: { status: "rejected", accepted: false } },
    { review_status: "published", decision: {} },
  ])("preserves historical read-only behavior for review status or a nonempty decision", async history => {
    const candidate = {
      job_id: "historical-prompt", candidate_kind: "execution", execution_profile_id: "context-history",
      ...history, evaluation: { recommended_publish: false, replay: { verdict: "inconclusive" } },
    };
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/detail") ? candidate : { candidates: [candidate] });
    const view = render(<CandidateReviewView active canManage />);
    fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(screen.queryByRole("button", { name: "重新评估" })).toBeNull();
    expect(screen.queryByRole("button", { name: "驳回" })).toBeNull();
    await waitFor(() => expect(mocks.modal.mock.lastCall?.[0]).toMatchObject({ open: true, readOnly: true, cand: candidate }));
    view.unmount();
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
});
