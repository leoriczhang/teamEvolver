import type { EvalResult } from "@/features/replay/api/client";
import type { ExecutionPlan, ExecutionRelease } from "@/features/skills/api/client";

export interface ExecutionReleaseDetail {
  profile_id: string;
  current_release_id: string;
  release: ExecutionRelease & {
    candidate_revision?: number;
    actor?: string;
    document: {
      execution_plan: ExecutionPlan;
      skill_dependencies?: Array<{ name: string; version: number; tree_sha256: string }>;
      origin?: string;
    };
  };
  validation: {
    status: "available" | "not_applicable" | "unavailable";
    reason?: string;
    run_id?: string;
    payload_hash?: string;
    evaluation_hash?: string;
    candidate_id?: string;
    candidate_revision?: number;
    source_release_id?: string;
    baseline_execution_plan?: ExecutionPlan | null;
    evaluation?: (EvalResult & {
      validation_verdict?: "accept" | "reject" | "inconclusive";
      execution_gate?: { status?: string; reason?: string };
    }) | null;
  };
}

export const executionProfilesUrl = "/api/skill-evolution/execution-profiles";
export const profileUrl = (id: string) => `${executionProfilesUrl}/${encodeURIComponent(id)}`;
export const releaseUrl = (profileId: string, releaseId: string) => (
  `${profileUrl(profileId)}/releases/${encodeURIComponent(releaseId)}`
);
