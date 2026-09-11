// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import PromptStudioView from "./PromptStudioView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("keeps the skill pipeline editable when service-wide settings are unavailable for a tenant", async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/prompt-studio/pipeline") {
      return {
        nodes: [
          {
            id: "ingest",
            label: "会话入队 Ingest",
            kind: "io",
            description: "输入会话",
          },
          {
            id: "summarize",
            label: "会话总结 Summarize",
            kind: "llm",
            prompt_id: "summarize",
            description: "总结会话",
          },
          {
            id: "judge",
            label: "会话评分 Judge",
            kind: "llm",
            prompt_id: "judge",
            description: "评估会话",
          },
        ],
        edges: [
          { from: "ingest", to: "summarize" },
          { from: "summarize", to: "judge" },
        ],
      };
    }
    if (path === "/api/prompt-studio/prompts") {
      return {
        prompts: [
          {
            id: "summarize",
            label: "会话总结 Summarize",
            description: "总结会话",
            module: "teamEvolver.evolve.stages.summarize",
            symbol: "_SUMMARIZE_SESSION_SYSTEM",
            temperature: 0.2,
            max_tokens: 8192,
            model: "",
            settings_overridden: false,
            injects_shared_blocks: false,
            overridden: false,
            char_count: 14,
            default_char_count: 14,
          },
          {
            id: "judge",
            label: "会话评分 Judge",
            description: "评估会话",
            module: "teamEvolver.evolve.stages.judge",
            symbol: "_JUDGE_SYSTEM",
            temperature: 0.1,
            max_tokens: 8192,
            model: "",
            settings_overridden: false,
            injects_shared_blocks: false,
            overridden: false,
            char_count: 12,
            default_char_count: 12,
          },
        ],
      };
    }
    if (path === "/api/prompt-studio/sessions?limit=50") {
      return { sessions: [] };
    }
    if (path === "/api/evolve-settings") {
      throw Object.assign(
        new Error("service-wide settings require the default account; use tenant config"),
        { status: 409 },
      );
    }
    if (path === "/api/prompt-studio/prompts/summarize") {
      return {
        id: "summarize",
        label: "会话总结 Summarize",
        description: "总结会话",
        module: "teamEvolver.evolve.stages.summarize",
        symbol: "_SUMMARIZE_SESSION_SYSTEM",
        temperature: 0.2,
        max_tokens: 8192,
        model: "",
        default_temperature: 0.2,
        default_max_tokens: 8192,
        settings_overridden: false,
        injects_shared_blocks: false,
        variables: ["session"],
        overridden: false,
        default_prompt: "DEFAULT SUMMARY PROMPT",
        effective_prompt: "CURRENT SUMMARY PROMPT",
        expanded_prompt: "CURRENT SUMMARY PROMPT",
      };
    }
    if (path === "/api/prompt-studio/prompts/judge") {
      return {
        id: "judge",
        label: "会话评分 Judge",
        description: "评估会话",
        module: "teamEvolver.evolve.stages.judge",
        symbol: "_JUDGE_SYSTEM",
        temperature: 0.1,
        max_tokens: 8192,
        model: "",
        default_temperature: 0.1,
        default_max_tokens: 8192,
        settings_overridden: false,
        injects_shared_blocks: false,
        variables: ["session"],
        overridden: false,
        default_prompt: "DEFAULT JUDGE PROMPT",
        effective_prompt: "CURRENT JUDGE PROMPT",
        expanded_prompt: "CURRENT JUDGE PROMPT",
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });

  render(
    <PromptStudioView
      active
      user={{ id: "admin", display_name: "Admin", role: "admin" }}
    />,
  );

  const stageNode = await screen.findByTitle("评估会话");
  fireEvent.click(stageNode);

  expect(await screen.findByDisplayValue("CURRENT JUDGE PROMPT")).toBeVisible();
  expect(screen.queryByText("链路加载中…")).not.toBeInTheDocument();
  await waitFor(() => {
    expect(api).toHaveBeenCalledWith("/api/prompt-studio/prompts/judge");
  });
});
