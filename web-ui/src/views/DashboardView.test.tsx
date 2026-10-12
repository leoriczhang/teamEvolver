// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type Candidate, type EvalResult } from "@/api/client";
import DashboardView from "./DashboardView";

vi.mock("@/api/client", async (original) => ({ ...await original<typeof import("@/api/client")>(), api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
vi.mock("@/hooks/useSessionList", () => ({ useSessionList: () => ({ rows: [], page: 1, total: 0, skillCounts: {}, loading: false, reload: vi.fn(), setPage: vi.fn() }) }));
vi.mock("@/components/session/SessionTable", () => ({ default: () => null }));
vi.mock("@/components/session/SessionFilters", () => ({ default: () => null }));
vi.mock("./dashboard/SkillVersionModal", () => ({ default: () => null }));
vi.mock("./skills/SkillEditModal", () => ({ default: () => null }));
vi.mock("./dashboard/SessionModal", () => ({ default: () => null }));
vi.mock("./dashboard/CandidateModal", () => ({ default: (props: { open: boolean; onUpdated: (candidate: Candidate) => void }) => props.open ?
  <button onClick={() => props.onUpdated({ job_id: "mined-1", skill_name: "mined-skill", evaluation: undefined })}>save candidate edit</button> : null }));
vi.mock("./dashboard/CandidateFeedback", () => ({ default: () => null }));
vi.mock("./dashboard/SetupChecklist", () => ({ default: () => null }));
vi.mock("./datasets/DatasetSelectionDialog", () => ({ default: () => null }));

beforeEach(() => { vi.mocked(api).mockReset(); vi.spyOn(window, "confirm").mockReturnValue(true); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function show(evaluation: EvalResult) {
  const candidate: Candidate = { job_id: "mined-1", skill_name: "mined-skill", evaluation };
  vi.mocked(api).mockImplementation(async (path) => {
    if (path.startsWith("/api/skill-candidates?")) return { candidates: [candidate], total: 1 } as any;
    if (path.includes("/evaluate")) return evaluation as any;
    if (path.endsWith("/detail")) return candidate as any;
    if (path.endsWith("/validate")) return { status: "published" } as any;
    return {} as any;
  });
  render(<DashboardView active onNavigate={vi.fn()} />);
}

it("uses the same skipped status and publish guard in the overview while allowing retry", async () => {
  show({ status: "skipped", recommended_publish: true, cached: true, replay: { reason: "ReplayUnsupported", case_count: 0 } });
  const row = (await screen.findByText("mined-skill")).closest("tr")!;
  expect(within(row).getByText("已跳过")).toBeVisible();
  expect(within(row).getByText("ReplayUnsupported")).toBeVisible();
  expect(within(row).getByText("未完成回放")).toBeVisible();
  expect(row).not.toHaveTextContent(/已完成|持平/);
  const publish = within(row).getByRole("button", { name: "按回放发布" });
  expect(publish).toBeDisabled();
  fireEvent.click(publish);
  expect(api).not.toHaveBeenCalledWith(expect.stringContaining("/validate"), expect.anything());
  expect(within(row).getByRole("button", { name: "强制发布" })).toBeEnabled();
  fireEvent.click(within(row).getByRole("button", { name: "重新评估" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-candidates/mined-1/evaluate?refresh=true", { method: "POST" }));
});

it("allows auto publish only when completed replay recommends it", async () => {
  show({ status: "completed", recommended_publish: true, replay: { status: "completed", case_count: 1 } });
  const publish = await screen.findByRole("button", { name: "按回放发布" });
  expect(publish).toBeEnabled();
  fireEvent.click(publish);
  await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-candidates/mined-1/validate", expect.objectContaining({ body: '{"mode":"auto"}' })));
});

it("invalidates the overview replay cache after saving candidate edits", async () => {
  show({ status: "completed", recommended_publish: true, replay: { status: "completed", case_count: 1 } });
  const skill = await screen.findByText("mined-skill");
  expect(screen.getByRole("button", { name: "按回放发布" })).toBeEnabled();
  fireEvent.click(skill);
  fireEvent.click(await screen.findByRole("button", { name: "save candidate edit" }));
  expect(screen.getByRole("button", { name: "按回放发布" })).toBeDisabled();
  expect(screen.getByText("待评估")).toBeVisible();
});
