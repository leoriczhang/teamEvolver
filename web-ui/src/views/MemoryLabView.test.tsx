// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import MemoryLabView from "./MemoryLabView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/views/OpenVikingWorkspaceShell", () => ({ MemoryInjectionCompare: () => null }));
afterEach(cleanup);

it("does not read Memory files when the connection is disabled", async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/users") return { users: [{ id: "alice", role: "admin" }] };
    if (path.startsWith("/api/openviking/workspace/config")) return {
      enabled: false,
      scopes: { personal_memory: { root_uri: "viking://user/alice/memories" } },
    };
    return { entries: [] };
  });
  render(<MemoryLabView active user={{ id: "alice", role: "admin" }} />);
  await screen.findByText(/OpenViking.*endpoint/);
  expect(vi.mocked(api).mock.calls.filter(([path]) => path.startsWith("/api/openviking/workspace/tree"))).toHaveLength(0);
});
