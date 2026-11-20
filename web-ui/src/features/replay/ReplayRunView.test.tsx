// @vitest-environment jsdom
import { render,screen } from "@testing-library/react";
import { describe,expect,it,vi } from "vitest";
import ReplayRunView from "./ReplayRunView";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ api: request }));

describe("saved Skill A/B run", () => {
  it("loads the real run and keeps completion separate from the effect verdict", async () => {
    request.mockResolvedValueOnce({
      status: "succeeded", subject_id: "core", total: 1, completed: 1,
      result: { verdict: "inconclusive", reason: "no measured improvement", cases: [{
        baseline: { final_response: "Baseline artifact", execution_snapshot: {
          session_id: "baseline-session", agent_id: "pinned-agent", agent_version: "10",
          environment_id: "env", environment_config_sha256: "same-env",
          runtime_tree_sha256: "same-runtime", agent_override_fields: ["skills"],
        } },
        candidate: { final_response: "Candidate artifact" },
      }] },
    });
    const view = render(<ReplayRunView runId="skill:real" active onBack={() => {}} />);
    expect(await screen.findByText("执行完成")).toBeTruthy();
    expect(screen.getByText("效果结论：无法确定")).toBeTruthy();
    expect(screen.getByText("baseline-session")).toBeTruthy();
    expect(screen.getByText("Candidate artifact")).toBeTruthy();
    expect(request).toHaveBeenCalledWith("/api/replays/skill/skill%3Areal", expect.anything());
    view.unmount();
  });
});
