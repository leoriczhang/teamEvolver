// @vitest-environment jsdom
import { api,waitCloudSkill } from "@/api/client";
import { runSkillCommand } from "./skillJobs";
import { afterEach,describe,expect,it,vi } from "vitest";

vi.mock("@/api/client", () => ({
  api: vi.fn(), waitCloudSkill: vi.fn(),
  tenantHeaders: (_path: string, initial?: HeadersInit) => new Headers(initial),
}));
afterEach(() => vi.resetAllMocks());

describe("cloud publication completion", () => {
  it("waits for imported cloud Skills before returning transfer success", async () => {
    vi.mocked(api)
      .mockResolvedValueOnce({ job_id: "transfer", status_url: "/api/jobs/transfer" })
      .mockResolvedValueOnce({ status: "succeeded" })
      .mockResolvedValueOnce({ result: { imported: [{ name: "notes", status: "accepted", job_id: "ma-skill:one" }] } });
    let complete!: (result: Record<string, unknown>) => void;
    vi.mocked(waitCloudSkill).mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    let settled = false;
    const result = runSkillCommand<any>("/api/skills/import", { method: "POST" }, "/api/skills/transfer")
      .then(value => { settled = true; return value; });
    await vi.waitFor(() => expect(waitCloudSkill).toHaveBeenCalledOnce());
    expect(settled).toBe(false);
    complete({ name: "notes", version: 3, status: "published", readback_verified: true });
    expect((await result).imported[0]).toMatchObject({ name: "notes", version: 3, status: "updated" });
  });
});
