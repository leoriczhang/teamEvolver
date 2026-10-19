// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ReplayDatasetResult } from "@/features/replay/api/client";
import CandidateReplayEvidence,{ EfficiencyTable } from "./CandidateReplayEvidence";

function dataset(index: number): ReplayDatasetResult {
  const input = {
    case_id: `case-${index}`, query: `任务 ${index}`, skill_ids: ["demo"], checks: [],
    materials: [], provenance: {}, replay: { progressive_disclosure: {} }, metadata: {},
  };
  const branch = {
    interaction_turns: 1, tool_call_count: 2, total_tokens: 800,
    interactions: [{ prompt: `用户请求 ${index}`, response: `完整回复 ${index}` }],
    checklist_report: { total: 1, satisfied_count: 1, items: [
      { id: "R1", text: `完成条件 ${index}`, satisfied: true, evidence: `证据 ${index}` },
    ] },
  };
  return {
    dataset_id: `ds-${index}`, name: `数据集 ${index}`, revision: 1, total: 1, completed: 1,
    selection: index ? "historical" : "current", inputs: [input],
    result: { verdict: "accept", cases: [{ case_id: input.case_id, baseline: branch, candidate: branch }] },
  };
}

describe("Candidate Dataset evidence", () => {
  it("shows all Datasets including history and every conversation and checklist", () => {
    const view = render(<CandidateReplayEvidence
      datasets={[0, 1, 2, 3].map(dataset)}
      sampling={{ historical_requested: 3, historical_selected: 3 }}
    />);
    expect(screen.getByText("回放覆盖 4 个数据集 · 4 个 Case")).toBeTruthy();
    for (let index = 0; index < 4; index++) {
      expect(screen.getByText(`数据集 ${index}`)).toBeTruthy();
      expect(screen.getAllByText(`完整回复 ${index}`).length).toBe(2);
      expect(screen.getAllByText(`证据 ${index}`).length).toBe(2);
    }
    view.unmount();
  });

  it("renders missing metrics as unavailable", () => {
    const view = render(<EfficiencyTable efficiency={{
      dimensions: { total_tokens: {
        baseline: "unavailable", candidate: 800, delta: null, reduction_ratio: null, winner: "unavailable",
      } },
    }} />);
    expect(screen.queryByText("NaN")).toBeNull();
    expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
    view.unmount();
  });
});
