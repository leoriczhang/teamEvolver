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

it("loads the skill pipeline and prompts for editing", async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/skill-evolution/pipeline") {
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
    if (path === "/api/skill-evolution/prompts") {
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
    if (path === "/api/skill-evolution/session-samples?limit=50") {
      return { sessions: [] };
    }
    if (path === "/api/skill-evolution/settings") {
      return {
        evolve: {
          use_session_judge: true,
          publish_mode: "validated",
          validation_max_rejections: 1,
          human_review_enabled: true,
          human_review_timeout_seconds: 86400,
          interval_seconds: 600,
          evidence_enabled: true,
          evidence_max_entries: 400,
          evidence_recent_limit: 20,
          evidence_historical_limit: 20,
          evidence_replay_cases_per_window: 1,
          evidence_change_debt_threshold: 3,
          dataset_synthesis_enabled: true,
          dataset_test_cases: 2,
          dataset_min_requirements: 12,
          dataset_max_requirements: 24,
          dataset_disclosure_batch_size: 4,
          candidate_coalesce_enabled: true,
          bundle_text_extensions: [".py", ".sh"],
          bundle_max_file_bytes: 262144,
          bundle_max_prompt_bytes: 786432,
          bundle_allow_delete: true,
          bundle_static_checks_enabled: true,
        },
        validation: {
          enabled: true,
          mode: "true_replay",
          idle_after_seconds: 300,
          poll_interval_seconds: 60,
          max_jobs_per_day: 5,
          max_concurrency: 1,
          required_results: 3,
          required_approvals: 2,
        },
      };
    }
    if (path === "/api/skill-evolution/prompts/summarize") {
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
    if (path === "/api/skill-evolution/prompts/judge") {
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
    expect(api).toHaveBeenCalledWith("/api/skill-evolution/prompts/judge");
  });
});
