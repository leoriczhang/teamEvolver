// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type Candidate, type EvalResult } from "@/api/client";
import { toastErr } from "@/lib/toast";
import CandidateReviewView from "./CandidateReviewView";

vi.mock("@/api/client", async (original) => ({ ...await original<typeof import("@/api/client")>(), api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
vi.mock("./dashboard/CandidateModal", () => ({ default: () => null }));
vi.mock("./dashboard/CandidateFeedback", () => ({ default: () => null }));

beforeEach(() => { vi.mocked(api).mockReset(); vi.spyOn(window, "confirm").mockReturnValue(true); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function setup(evaluation: EvalResult) {
  const candidate: Candidate = { job_id: "mined-1", skill_name: "mined-skill", evaluation };
  vi.mocked(api).mockImplementation(async (path) => {
    if (path.endsWith("/validate")) return { status: "published" } as any;
    if (path.includes("/evaluate")) return evaluation as any;
    return { candidates: [candidate] } as any;
  });
  render(<CandidateReviewView active canManage />);
}

it("shows cached skipped replay as incomplete, excludes it from completed totals, and allows retry", async () => {
  setup({ status: "evaluated", cached: true, recommended_publish: false, replay: { status: "skipped", reason: "ReplayUnsupported: missing runtime", case_count: 0, cases: [] } });
  expect(await screen.findByText("mined-skill")).toBeVisible();
  const row = screen.getByText("mined-skill").closest("tr")!;
  expect(within(row).getByText("已跳过")).toBeVisible();
  expect(within(row).getByText("ReplayUnsupported: missing runtime")).toBeVisible();
  expect(within(row).getByText("未完成回放")).toBeVisible();
  expect(screen.queryByText("持平 / 有增加")).not.toBeInTheDocument();
  expect(screen.getByText("已完成评估").parentElement).toHaveTextContent("0");
  const publish = within(row).getByRole("button", { name: "按回放发布" });
  expect(publish).toBeDisabled();
  fireEvent.click(publish);
  expect(api).not.toHaveBeenCalledWith(expect.stringContaining("/validate"), expect.anything());
  expect(within(row).getByRole("button", { name: "强制发布" })).toBeEnabled();
  fireEvent.click(within(row).getByRole("button", { name: "重新评估" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-candidates/mined-1/evaluate?refresh=true", { method: "POST" }));
  await waitFor(() => expect(toastErr).toHaveBeenCalledWith("回放未完成", "ReplayUnsupported: missing runtime"));
});

it.each([
  { status: "completed", recommended_publish: true, replay: { case_count: 0 } },
  { status: "failed", recommended_publish: true, replay: { case_count: 1, reason: "runtime failed" } },
  { status: "completed", recommended_publish: false, replay: { case_count: 1 } },
] satisfies EvalResult[]) ("blocks auto publish for %j", async (evaluation) => {
  setup(evaluation);
  expect(await screen.findByRole("button", { name: "按回放发布" })).toBeDisabled();
});

it("enables recommended publication only for observed completed replay and preserves explicit force confirmation", async () => {
  setup({ status: "completed", recommended_publish: true, replay: { status: "completed", case_count: 1 } });
  const publish = await screen.findByRole("button", { name: "按回放发布" });
  expect(publish).toBeEnabled();
  expect(screen.getByText("已完成评估").parentElement).toHaveTextContent("1");
  fireEvent.click(publish);
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-candidates/mined-1/validate", expect.objectContaining({ body: '{"mode":"auto"}' })));
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("确认按 True Replay 发布"));
});

it("keeps admin force publish available for unsupported replay with explicit confirmation", async () => {
  setup({ status: "skipped", replay: { reason: "ReplayUnsupported" } });
  fireEvent.click(await screen.findByRole("button", { name: "强制发布" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-candidates/mined-1/validate", expect.objectContaining({ body: '{"mode":"force"}' })));
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("确认强制发布"));
});
