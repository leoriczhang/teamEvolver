// @vitest-environment jsdom
import { render,screen } from "@testing-library/react";
import { beforeEach,describe,expect,it,vi } from "vitest";
import SessionModal from "./SessionModal";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));

describe("SessionModal archived conversation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.api.mockResolvedValue({
      summary: { title: "Original task", user_alias: "alice", status: "archived", num_turns: 1 },
      meta: { trace_id: "trace-1" },
      turns_available: true,
      turns_source: "archive",
      turns: [{
        turn_num: 1,
        prompt_text: "Please inspect the repository",
        response_text: "Inspection completed",
        injected_skills: ["code-review"],
        used_skills: ["code-review"],
        tool_calls: [{ id: "call-1", function: { name: "read_file", arguments: "{\"path\":\"README.md\"}" } }],
        tool_results: [{ tool_call_id: "call-1", tool_name: "read_file", content: "Repository overview" }],
      }],
    });
  });

  it("visualizes the archived original conversation and tool evidence", async () => {
    const view = render(
      <SessionModal sid="session-1" initialTab="detail" open onClose={vi.fn()} />
    );
    expect(await screen.findByText("会话原文")).toBeTruthy();
    expect(screen.getByText("Please inspect the repository")).toBeTruthy();
    expect(screen.getByText("Inspection completed")).toBeTruthy();
    expect(screen.getAllByText(/read_file/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Repository overview/)).toBeTruthy();
    expect(screen.getByText("来自归档快照")).toBeTruthy();
    const dialog = document.querySelector('[data-slot="dialog-content"]');
    expect(dialog?.className).toContain("!max-w-[1280px]");
    view.unmount();
  });
});
