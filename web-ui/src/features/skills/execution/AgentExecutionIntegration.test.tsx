// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import AgentExecutionIntegration from "./AgentExecutionIntegration";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("AgentExecutionIntegration", () => {
  it("loads actual integration metadata on demand and checks a named archived Session without starting work", async () => {
    vi.mocked(api).mockImplementation(async path => path.includes("session-match")
      ? { matched: true, reason: "Matched the actual template and context", release_id: "checked-release", composition_hash: "checked-hash", targets: [{}], eligible_for_evolution: true }
      : { profile_id: "business:one", current_release_id: "release-one", composition_hash: "hash", variables: [{ name: "rules", managed_context_component_ids: ["context-one"] }],
        requirements: [], metadata: { teamevolver_execution: { execution_profile_id: "business:one" } } });
    render(<AgentExecutionIntegration profileId="business:one" />);
    expect(api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("接入真实 Agent 的会话"));
    expect(await screen.findByText("rules：1 个受管理的上下文取值")).toBeVisible();
    fireEvent.change(screen.getByLabelText("已归档的 Session ID"), { target: { value: "session:1/2" } });
    fireEvent.click(screen.getByRole("button", { name: "检查会话匹配" }));
    expect(await screen.findByRole("status")).toHaveTextContent("会话已匹配此执行配置");
    expect(screen.getByRole("status")).toHaveTextContent("本次核对发布：checked-release · 组合哈希：checked-hash");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/skill-evolution/execution-profiles/business%3Aone/session-match?session_id=session%3A1%2F2"));
    expect(vi.mocked(api).mock.calls.every(([, options]) => !options?.method)).toBe(true);
  });
});
