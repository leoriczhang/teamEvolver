// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, hydrateCandidates, type CandidateFeedback } from "@/api/client";
import { toastErr } from "@/lib/toast";
import CandidateFeedbackControls from "./CandidateFeedback";

vi.mock("@/api/client", async (original) => ({
  ...await original<typeof import("@/api/client")>(), api: vi.fn(),
}));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const empty: CandidateFeedback = {
  reviewed: false, adopted: false, rejected: false, version: 0, history: [],
};
const saved: CandidateFeedback = {
  reviewed: true, adopted: false, rejected: false, version: 1,
  history: [{ field: "reviewed", value: true, actor_id: "alice", actor_name: "Alice",
    at: "2026-09-18T08:00:00Z", candidate_revision: 1, version: 1 }],
};

it("saves only the changed checkbox and displays the persisted state and history", async () => {
  vi.mocked(api).mockResolvedValue({ feedback: saved });
  function View() {
    const [feedback, setFeedback] = useState(empty);
    return <CandidateFeedbackControls jobId="job" feedback={feedback} showHistory onSaved={(_, value) => setFeedback(value)} />;
  }
  render(<View />);
  fireEvent.click(screen.getByLabelText("已审阅"));
  await waitFor(() => expect(screen.getByLabelText("已审阅")).toBeChecked());
  expect(screen.getByLabelText("已采纳")).not.toBeChecked();
  expect(screen.getByLabelText("已驳回")).not.toBeChecked();
  expect(screen.getByLabelText("已采纳")).toBeDisabled();
  expect(screen.getByLabelText("已驳回")).toBeDisabled();
  expect(api).toHaveBeenCalledWith("/api/skill-candidates/job/feedback", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ reviewed: true }),
  }));
  fireEvent.click(screen.getByText("操作记录（1）"));
  expect(screen.getByText("Alice")).toBeVisible();
  expect(screen.getByText("勾选「已审阅」")).toBeVisible();
});

it("keeps persisted state after an unsuccessful save", async () => {
  vi.mocked(api).mockRejectedValue(new Error("disk full"));
  const onSaved = vi.fn();
  render(<CandidateFeedbackControls jobId="job" feedback={empty} onSaved={onSaved} />);
  fireEvent.click(screen.getByLabelText("已审阅"));
  await waitFor(() => expect(toastErr).toHaveBeenCalledWith("保存标记失败", "disk full"));
  expect(screen.getByLabelText("已审阅")).not.toBeChecked();
  expect(onSaved).not.toHaveBeenCalled();
  expect(screen.getByLabelText("已审阅")).toBeEnabled();
});

it("disables all controls until a save completes and supports unchecking reviewed", async () => {
  let resolve!: (value: { feedback: CandidateFeedback }) => void;
  vi.mocked(api).mockReturnValue(new Promise((done) => { resolve = done; }));
  const onSaved = vi.fn();
  render(<CandidateFeedbackControls jobId="job" feedback={saved} onSaved={onSaved} />);
  fireEvent.click(screen.getByLabelText("已审阅"));
  expect(screen.getByLabelText("已审阅")).toBeDisabled();
  expect(screen.getByLabelText("已采纳")).toBeDisabled();
  expect(screen.getByLabelText("已驳回")).toBeDisabled();
  expect(api).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ body: '{"reviewed":false}' }));
  resolve({ feedback: { ...saved, reviewed: false, version: 2 } });
  await waitFor(() => expect(onSaved).toHaveBeenCalled());
});

it("shows processed candidates as read-only", () => {
  render(<CandidateFeedbackControls jobId="job" feedback={saved} readOnly onSaved={vi.fn()} />);
  expect(screen.getByLabelText("已审阅")).toBeChecked();
  expect(screen.getByLabelText("已审阅")).toBeDisabled();
  expect(screen.getByLabelText("已采纳")).toBeDisabled();
  expect(screen.getByLabelText("已驳回")).toBeDisabled();
});

it("does not overwrite a saved mark when an older poll arrives", () => {
  const details = { job: { job_id: "job", candidate_skill_md: "content", feedback: saved } };
  const result = hydrateCandidates([{ job_id: "job", feedback: empty }], details);
  expect(result[0].feedback).toEqual(saved);
  expect(result[0].candidate_skill_md).toBe("content");
  const newer = { ...saved, adopted: true, version: 2 };
  expect(hydrateCandidates([{ job_id: "job", feedback: newer }], details)[0].feedback).toEqual(newer);
});
