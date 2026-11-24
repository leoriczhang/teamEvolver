// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SkillVersionModal from "./SkillVersionModal";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
vi.stubGlobal("ResizeObserver", class {
  observe() {}
  unobserve() {}
  disconnect() {}
});
afterEach(cleanup);

it("labels every version and switches to the selected version's recorded origin", async () => {
  const manual = { kind: "manual" as const, source: "console", actor: "alice", base_version: 0 };
  const te = { kind: "te" as const, actor: "reviewer", base_version: 1, candidate_id: "candidate-2" };
  const history = [
    { version: 3, provenance: { kind: "unknown" as const } },
    { version: 2, timestamp: "2026-11-23T10:00:00Z", provenance: te },
    { version: 1, timestamp: "2026-11-21T10:00:00Z", provenance: manual },
  ];
  mocks.api.mockImplementation((url: string) => {
    const version = Number(url.substring(url.lastIndexOf("/") + 1));
    return Promise.resolve({
      version, current_version: 3, versions: [3, 2, 1], history,
      provenance: history.find(row => row.version === version)?.provenance,
      content: `Content ${version}`,
    });
  });
  render(<SkillVersionModal name="demo" initialVersion={2} open onClose={vi.fn()} onRolled={vi.fn()} />);
  await screen.findByText("candidate-2");
  expect(screen.getByRole("button", { name: "v3 ·当前 · 来源未记录" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "v2 · TE 修改并发布" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "v1 · 用户手动上传 / 修改" })).toBeTruthy();
  expect(screen.getByText("reviewer")).toBeTruthy();
  expect(screen.getByText("基于 Skill v1")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "v1 · 用户手动上传 / 修改" }));
  await screen.findByText("alice");
  expect(screen.queryByText("candidate-2")).toBeNull();
  expect(screen.getByText("新建技能 · 无基线版本")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "v3 ·当前 · 来源未记录" }));
  await waitFor(() => expect(screen.getByText("Content 3")).toBeTruthy());
  expect(screen.queryByText("alice")).toBeNull();
});
