// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useReplayPanel } from "./useReplayPanel";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api, tenantHeaders: () => new Headers() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
const dataset = {
  schema_version: "team-replay.dataset", dataset_id: "draft", name: "Draft",
  skills: [{ skill_id: "demo" }], source: {}, metadata: {}, revision: 3, cases: [{
    case_id: "one", query: "Task", skill_ids: ["demo"], checks: [{ id: "r", kind: "output", text: "Done" }],
    materials: [], provenance: {}, metadata: {}, replay: { progressive_disclosure: { batch_size: 4 } },
  }],
};
const user = { id: "alice", display_name: "Alice", role: "admin" as const };
beforeEach(() => {
  mocks.api.mockReset();
  mocks.api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === "/api/replay-lab/skills") return { skills: [{ name: "demo" }] };
    if (path === "/api/replay-lab/skills/demo") return { name: "demo", skill_md: "original", sha256: "fixed-hash" };
    if (path.startsWith("/api/replay-lab/datasets?")) return { datasets: [dataset] };
    if (path.startsWith("/api/replay-lab/runs?")) return { runs: [] };
    if (path.endsWith("/clone")) return { ...dataset, dataset_id: "copy" };
    if (path === "/api/replay-lab/datasets/synthesize") return { job_id: "synth", status_url: "/api/jobs/synth" };
    if (path === "/api/jobs/synth") return { status: "succeeded" };
    if (path.endsWith("/jobs/synth/result")) return { result: { datasets: [dataset], count: 1, source_session_count: 1 } };
    if (path === "/api/replay-lab/runs" && init?.method === "POST") {
      return { run_id: "run", job_id: "run", dataset_id: "draft", status: "queued" };
    }
    return {};
  });
});
afterEach(cleanup);

it("saves against the loaded hash and clones with an empty body", async () => {
  const { result } = renderHook(() => useReplayPanel({ active: true, user }));
  await waitFor(() => expect(result.current.skill?.sha256).toBe("fixed-hash"));
  await act(() => result.current.saveSkill());
  const save = mocks.api.mock.calls.find(([path]) => path.endsWith("/save"));
  expect(JSON.parse(String(save?.[1]?.body))).toEqual({
    candidate_skill_md: "original", expected_sha256: "fixed-hash",
  });
  await act(() => result.current.cloneDataset(dataset as any));
  const clone = mocks.api.mock.calls.find(([path]) => path.endsWith("/clone"));
  expect(JSON.parse(String(clone?.[1]?.body))).toEqual({});
});

it("follows synthesis job results and keeps accepted replay queued", async () => {
  const { result } = renderHook(() => useReplayPanel({ active: true, user, externalFiles: { "ref.md": "draft" } }));
  await waitFor(() => expect(result.current.selectedDatasetId).toBe("draft"));
  await act(() => result.current.synthesizeDatasets());
  expect(mocks.api).toHaveBeenCalledWith("/api/jobs/synth", expect.any(Object));
  expect(mocks.api).toHaveBeenCalledWith("/api/replay-lab/jobs/synth/result", expect.any(Object));
  await act(() => result.current.startRun());
  expect(result.current.activeRun?.status).toBe("queued");
  const call = mocks.api.mock.calls.find(([path, init]) => path === "/api/replay-lab/runs" && init?.method === "POST");
  expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
    skill_id: "demo", candidate_files: { "ref.md": "draft" },
  });
});
