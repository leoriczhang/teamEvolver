import { api } from "@/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { afterEach,expect,it,vi } from "vitest";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  tenantHeaders: () => new Headers({ "X-Tenant-Id": "original" }),
}));

afterEach(() => { vi.resetAllMocks(); vi.useRealTimers(); });

it("waits for the job and reads its result in the originally selected tenant", async () => {
  vi.useFakeTimers();
  vi.mocked(api)
    .mockResolvedValueOnce({ job_id: "job", status_url: "/api/jobs/job" })
    .mockResolvedValueOnce({ status: "running" })
    .mockResolvedValueOnce({ status: "succeeded" })
    .mockResolvedValueOnce({ result: { status: "evaluated", evaluation: { recommended_publish: true } } });
  const result = runSkillCommand("/api/skill-candidates/demo/evaluate", { method: "POST" });
  await vi.advanceTimersByTimeAsync(2000);
  expect(await result).toEqual({ recommended_publish: true });
  for (const [, init] of vi.mocked(api).mock.calls) {
    expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("original");
  }
});

it("reports persisted failure without claiming publication", async () => {
  vi.mocked(api)
    .mockResolvedValueOnce({ job_id: "job", status_url: "/api/jobs/job" })
    .mockResolvedValueOnce({ status: "failed", error_code: "retries_exhausted" });
  await expect(runSkillCommand("/api/skill-candidates/demo/validate", { method: "POST" }))
    .rejects.toThrow("retries_exhausted");
  expect(api).toHaveBeenCalledTimes(2);
});

it("reads pattern compilation from its own result endpoint after the worker succeeds", async () => {
  vi.mocked(api)
    .mockResolvedValueOnce({ job_id: "pattern", status_url: "/api/jobs/pattern" })
    .mockResolvedValueOnce({ status: "succeeded" })
    .mockResolvedValueOnce({ result: { compiled: true, skill_name: "demo", pattern_count: 2 } });
  const result = await runSkillCommand("/api/skill-experience-patterns/compile",
    { method: "POST", body: JSON.stringify({ skill: "demo" }) }, "/api/experience-sync");
  expect(result).toEqual({ compiled: true, skill_name: "demo", pattern_count: 2 });
  expect(vi.mocked(api).mock.calls[2][0]).toBe("/api/experience-sync/jobs/pattern/result");
});
