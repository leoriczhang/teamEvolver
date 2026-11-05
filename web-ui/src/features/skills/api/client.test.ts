import { describe, expect, it } from "vitest";
import { hydrateCandidates, type Candidate } from "./client";

describe("compact candidate polling", () => {
  const detail: Candidate = {
    job_id: "candidate", candidate_revision: 4,
    evaluation: { replay: { verdict: "inconclusive", cases: [{ case_id: "evidence" }] } },
  };

  it("keeps loaded Replay evidence for the same candidate revision", () => {
    const summary: Candidate = {
      job_id: "candidate", candidate_revision: 4,
      evaluation: { summary_only: true, replay: { verdict: "inconclusive" } },
    };
    const [result] = hydrateCandidates([summary], { candidate: detail });
    expect(result.evaluation?.replay?.cases).toEqual([{ case_id: "evidence" }]);
  });

  it("uses the new revision's verdict instead of stale loaded evidence", () => {
    const summary: Candidate = {
      job_id: "candidate", candidate_revision: 5,
      evaluation: { summary_only: true, replay: { verdict: "reject" } },
    };
    const [result] = hydrateCandidates([summary], { candidate: detail });
    expect(result.evaluation?.replay?.verdict).toBe("reject");
    expect(result.evaluation?.replay?.cases).toBeUndefined();
  });

  it("drops old content on revision changes and old evidence after reevaluation", () => {
    const cached = { ...detail, current_skill_md: "OLD", candidate_skill_md: "OLD CANDIDATE",
      updated_at: "2026-10-20" };
    for (const summary of [
      { job_id: "candidate", candidate_revision: 5, updated_at: "2026-10-20" },
      { job_id: "candidate", candidate_revision: 4, updated_at: "2026-10-23" },
    ]) {
      const [result] = hydrateCandidates([summary], { candidate: cached });
      expect(result.candidate_skill_md).toBeUndefined();
      expect(result.evaluation).toBeUndefined();
    }
  });
});
