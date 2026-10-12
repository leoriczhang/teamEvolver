import { describe, expect, it } from "vitest";
import type { EvalResult } from "@/api/client";
import { canPublishFromReplay, formatReplayMetric, replayConclusion, replayEvaluationState } from "./replay-evaluation";

const branches = [{ baseline: { response: "baseline", total_tokens: 0 }, candidate: { response: "candidate", total_tokens: 0 } }];

describe("True Replay completion and publish eligibility", () => {
  it.each([
    [undefined, "pending"],
    [{ status: "evaluated", replay: { cases: [] } }, "not_run"],
    [{ status: "completed", replay: { case_count: 0 } }, "not_run"],
    [{ status: "completed", replay: { case_count: 0, cases: branches } }, "not_run"],
    [{ status: "completed", replay: { case_count: 2 } }, "completed"],
    [{ status: "completed", replay: { cases: branches } }, "completed"],
    [{ status: "evaluated", replay: { cases: branches } }, "completed"],
    [{ status: "evaluated", replay: { cases: [{ baseline: {}, candidate: {} }] } }, "not_run"],
    [{ status: "evaluated", replay: { cases: [{ baseline: { instruction: "test" }, candidate: { instruction: "test" } }] } }, "not_run"],
    [{ status: "evaluated", replay: { cases: [{ baseline: { ok: false }, candidate: { response: "test" } }] } }, "failed"],
    [{ status: "evaluated", replay: { cases: [{ baseline: { status: "unsupported" }, candidate: { response: "test" } }] } }, "failed"],
    [{ status: "evaluated", replay: { cases: branches, reason: "ReplayUnsupported: missing runtime" } }, "not_run"],
    [{ status: "evaluated", replay: { status: "skipped", cases: branches } }, "skipped"],
    [{ status: "not_run", replay: { cases: branches } }, "not_run"],
    [{ status: "failed", replay: { cases: branches } }, "failed"],
    [{ status: "completed", replay: { case_count: 1, error: "runtime failed" } }, "failed"],
    [{ status: "completed", replay: { cases: [{ baseline: { response: "ok" }, candidate: { error: "tool failed" } }] } }, "failed"],
  ] as Array<[EvalResult | undefined, string]>) ("classifies %j as %s", (value, expected) => {
    expect(replayEvaluationState(value)).toBe(expected);
  });

  it("requires real completion and a positive recommendation, even for cached legacy results", () => {
    expect(canPublishFromReplay({ status: "skipped", recommended_publish: true })).toBe(false);
    expect(canPublishFromReplay({ status: "evaluated", recommended_publish: true, replay: { cases: [] } })).toBe(false);
    const completed: EvalResult = { status: "completed", recommended_publish: true, replay: { case_count: 1 } };
    expect(canPublishFromReplay(completed)).toBe(true);
    expect(canPublishFromReplay({ ...completed, recommended_publish: false })).toBe(false);
    expect(canPublishFromReplay({ ...completed, replay: { case_count: 1, decision_policy: { accepted: false } } })).toBe(false);
    expect(canPublishFromReplay({ ...completed, replay: { case_count: 1, cases: [{ baseline: { error: "failed" }, candidate: { response: "ok" } }] } })).toBe(false);
  });

  it("does not describe missing measurements or unexecuted replay as tied", () => {
    expect(replayConclusion({ status: "skipped", recommended_publish: false })).toBe("未完成回放");
    expect(replayConclusion({ status: "completed", replay: { case_count: 1 } })).toBe("指标未观测");
    expect(replayConclusion({ status: "completed", replay: { case_count: 1, decision_policy: { decision_basis: "candidate_checklist_incomplete" } } })).toBe("完成条件未通过");
  });
});

it.each([undefined, null, "unavailable", "", " ", Number.NaN, Infinity, -Infinity, false])("shows an unknown metric %j as unobserved", (value) => {
  expect(formatReplayMetric(value)).toBe("未观测");
});

it("preserves observed zero counters", () => {
  expect(formatReplayMetric(0)).toBe("0");
  expect(formatReplayMetric("0")).toBe("0");
  expect(formatReplayMetric(9000)).toBe((9000).toLocaleString());
});
