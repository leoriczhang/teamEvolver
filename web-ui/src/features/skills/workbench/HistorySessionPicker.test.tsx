// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,expect,it,vi } from "vitest";
import HistorySessionPicker from "./HistorySessionPicker";
import { RunDetail } from "./ReplayPanelShared";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
afterEach(cleanup);

it("selects archived Sessions with actual versions and generates using their IDs", async () => {
  mocks.api.mockResolvedValue({ sessions: [{ session_id: "s1", title: "历史任务", used_skills: ["demo"],
    used_skill_versions: [{ name: "demo", version: "4" }] }], has_more: false });
  const generate = vi.fn().mockResolvedValue(undefined);
  render(<HistorySessionPicker skillName="demo" busy={false} onGenerate={generate} />);
  fireEvent.click(screen.getByRole("button", { name: "从历史 Session 生成数据集" }));
  await screen.findByText("历史任务");
  expect(mocks.api.mock.calls[0][0]).toContain("skill=demo");
  expect(screen.getByText("demo · v4")).toBeVisible();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "生成数据集（1）" }));
  await waitFor(() => expect(generate).toHaveBeenCalledWith(["s1"]));
});

it("shows every replay Case, pinned versions and failure evidence", () => {
  render(<RunDetail run={{ run_id: "r", dataset_id: "d", status: "failed", error_code: "runtime_unavailable",
    skill_versions: { demo: 4 }, baseline_version: "4", result: { cases: [
      { baseline: { final_response: "Baseline one" }, candidate: { final_response: "Candidate one" } },
      { baseline: { final_response: "Baseline two" }, candidate: { error: "Missing authorization" } },
    ] } }} onRefresh={() => {}} />);
  expect(screen.getByText("demo · v4")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("runtime_unavailable");
  expect(screen.getByText("Case 2")).toBeVisible();
  expect(screen.getByText("Missing authorization")).toBeVisible();
});
