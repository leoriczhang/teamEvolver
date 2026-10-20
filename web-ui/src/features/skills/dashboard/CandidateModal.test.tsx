// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CandidateModal from "./CandidateModal";

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
});
