// @vitest-environment jsdom
import { cleanup,fireEvent,render,screen,within } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";
import SessionModal from "./SessionModal";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", () => ({ api: mocks.api }));
afterEach(cleanup);

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
    expect(screen.queryByText(/Repository overview/)).toBeNull();
    expect(screen.queryByText(/Tool Results/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 read_file" }));
    expect(screen.getByText(/Repository overview/)).toBeTruthy();
    expect(screen.getByText("来自归档快照")).toBeTruthy();
    const dialog = document.querySelector('[data-slot="dialog-content"]');
    expect(dialog?.className).toContain("!max-w-[1280px]");
    view.unmount();
  });

  it("shows separate historical versions for main and child Agents", async () => {
    mocks.api.mockResolvedValueOnce({
      summary: { title: "Versioned session" }, turns_available: true, turns: [],
      skill_versions: [
        { name: "notes", skill_id: "shared", version: "1", agent_id: "root", agent_name: "director",
          agent_role: "main", usage: "configured" },
        { name: "notes", skill_id: "shared", version: "2", agent_id: "child", agent_name: "writer",
          agent_role: "member", usage: "used" },
      ],
    });
    render(<SessionModal sid="versioned" initialTab="detail" open onClose={vi.fn()} />);
    const section = await screen.findByRole("region", { name: "Skill 版本" });
    const rows = within(section).getAllByRole("row");
    expect(rows[1].textContent).toContain("v1");
    expect(rows[1].textContent).toContain("主 Agent");
    expect(rows[1].textContent).toContain("已挂载");
    expect(rows[2].textContent).toContain("v2");
    expect(rows[2].textContent).toContain("writer");
    expect(rows[2].textContent).toContain("有调用记录");
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith("/api/sessions/versioned/content");
  });

  it("shows missing version evidence explicitly", async () => {
    mocks.api.mockResolvedValueOnce({
      summary: { title: "Old session" }, turns: [],
      skill_versions: [{
        name: "notes", skill_id: null, version: null, agent_id: null, agent_name: "调用 Agent 未确认",
        agent_role: "unknown", usage: "used", unknown_reason: "会话快照中缺少对应的 Skill 引用",
      }],
    });
    render(<SessionModal sid="old" initialTab="detail" open onClose={vi.fn()} />);
    const section = await screen.findByRole("region", { name: "Skill 版本" });
    expect(within(section).getByText("版本未知")).toBeTruthy();
    expect(within(section).getByText("会话快照中缺少对应的 Skill 引用")).toBeTruthy();
    expect(within(section).getByText("有调用记录")).toBeTruthy();
  });
});
