// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,within } from "@testing-library/react";
import { afterEach,describe,expect,it } from "vitest";
import SessionToolCalls from "./SessionToolCalls";

afterEach(cleanup);

describe("session tool call display", () => {
  it("decodes Unicode and newlines for reading while retaining the original arguments", () => {
    const raw = '{"file_path":"/workspace/report.md","content":"# \\u60ac\\u7591\\u77ed\\u5267\\n\\n\\u5236\\u4f5c\\u62a5\\u544a"}';
    render(<SessionToolCalls calls={[{ function: { name: "Write", arguments: raw } }]} />);
    expect(screen.getByText("/workspace/report.md")).toBeVisible();
    expect(screen.queryByText(/悬疑短剧/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 Write" }));
    const content = screen.getByText(/悬疑短剧/);
    expect(content.textContent).toBe("# 悬疑短剧\n\n制作报告");
    expect(content).toBeVisible();
    expect(screen.getByText(raw, { exact: true }).textContent).toBe(raw);
    fireEvent.click(screen.getByRole("button", { name: "收起第 1 次工具调用 Write" }));
    expect(screen.queryByText(/悬疑短剧/)).not.toBeInTheDocument();
  });

  it("reads object inputs and gives task lists a short summary", () => {
    render(<SessionToolCalls calls={[{
      name: "TodoWrite", input: { todos: [{ content: "创作完整剧本", status: "in_progress" }] },
    }]} />);
    expect(screen.getByText("1 项任务")).toBeVisible();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 TodoWrite" }));
    expect(screen.getAllByText(/创作完整剧本/).length).toBeGreaterThan(0);
  });

  it("preserves incomplete JSON and literal escape sequences without guessing", () => {
    const raw = '{"content":"\\u60ac", invalid';
    render(<SessionToolCalls calls={[{ function: { name: "Write", arguments: raw } }]} />);
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 Write" }));
    expect(screen.getAllByText(raw, { exact: true }).every((element) => element.textContent === raw)).toBe(true);
    expect(screen.queryByText(/悬/)).not.toBeInTheDocument();
  });

  it("paginates sixty calls and keeps the final call reachable in order", () => {
    render(<SessionToolCalls calls={Array.from({ length: 60 }, (_, index) => ({
      id: `call-${index}`, function: { name: "Read", arguments: JSON.stringify({ file_path: `/files/${index + 1}.md` }) },
    }))} />);
    const list = screen.getByRole("region", { name: "工具调用" });
    expect(within(list).getByText("工具调用（60） · 结果（0）")).toBeVisible();
    expect(within(list).getAllByRole("button", { name: /^展开第/ })).toHaveLength(10);
    expect(screen.queryByText("/files/60.md")).not.toBeInTheDocument();
    fireEvent.click(within(list).getByRole("button", { name: "6" }));
    expect(within(list).getByRole("button", { name: "展开第 60 次工具调用 Read" })).toBeVisible();
    expect(screen.getByText("/files/60.md")).toBeVisible();
    fireEvent.click(within(list).getByRole("button", { name: "展开第 60 次工具调用 Read" }));
    expect(screen.getByRole("button", { name: "收起第 60 次工具调用 Read" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(within(list).getByRole("button", { name: "1" }));
    expect(screen.getByRole("button", { name: "展开第 1 次工具调用 Read" })).toHaveAttribute("aria-expanded", "false");
  });

  it("matches results by call ID when repeated tools return out of order", () => {
    render(<SessionToolCalls calls={[
      { id: "read-a", function: { name: "Read", arguments: '{"path":"a.md"}' } },
      { id: "read-b", function: { name: "Read", arguments: '{"path":"b.md"}' } },
    ]} results={[
      { tool_call_id: "read-b", content: "B output" },
      { tool_call_id: "read-a", content: "A output" },
      { tool_call_id: "read-a", content: "A continuation" },
    ]} />);
    expect(screen.queryByText("A output")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 Read" }));
    expect(screen.getByText("A output")).toBeVisible();
    expect(screen.getByText("A continuation")).toBeVisible();
    expect(screen.queryByText("B output")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开第 2 次工具调用 Read" }));
    expect(screen.getByText("B output")).toBeVisible();
  });

  it("preserves failures, missing results, and unmatched result evidence", () => {
    render(<SessionToolCalls calls={[
      { id: "read-a", function: { name: "Read" } },
      { id: "read-b", function: { name: "Read" } },
    ]} results={[
      { tool_call_id: "read-a", content: "Permission denied", has_error: true },
      { tool_call_id: "unknown", tool_name: "Read", content: "Unmatched output" },
      { tool_name: "Read", content: "No ID output" },
    ]} />);
    expect(screen.getByText("调用出错")).toBeVisible();
    expect(screen.getByText("未匹配的结果（2）")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "展开第 1 次工具调用 Read" }));
    expect(screen.getByText("Permission denied")).toBeVisible();
    expect(screen.getByText("Permission denied")).toHaveClass("text-destructive");
    fireEvent.click(screen.getByRole("button", { name: "展开第 2 次工具调用 Read" }));
    expect(screen.getByText("该调用暂无归档结果。")).toBeVisible();
    fireEvent.click(screen.getByText("未匹配的结果（2）"));
    expect(screen.getByText("Unmatched output")).toBeVisible();
    expect(screen.getByText("No ID output")).toBeVisible();
  });

  it("retains results when no call exists and never guesses between duplicate call IDs", () => {
    const view = render(<SessionToolCalls calls={[]} results={[{ content: "Standalone output" }]} />);
    expect(screen.getByText("工具调用（0） · 结果（1）")).toBeVisible();
    fireEvent.click(screen.getByText("未匹配的结果（1）"));
    expect(screen.getByText("Standalone output")).toBeVisible();
    view.rerender(<SessionToolCalls calls={[{ id: "duplicate" }, { id: "duplicate" }]} results={[
      { tool_call_id: "duplicate", content: "Ambiguous output" },
    ]} />);
    expect(screen.getByText("Ambiguous output")).toBeVisible();
    expect(screen.getAllByText("暂无结果")).toHaveLength(2);
  });
});
