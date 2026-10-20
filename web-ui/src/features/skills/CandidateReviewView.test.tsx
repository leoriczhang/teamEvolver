// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CandidateReviewView from "./CandidateReviewView";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
vi.mock("@/features/skills/dashboard/CandidateModal", () => ({ default: () => null }));
vi.mock("@/features/skills/dashboard/CandidateFeedback", () => ({ default: () => null }));

describe("candidate replay state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
