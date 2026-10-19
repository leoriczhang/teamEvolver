// @vitest-environment jsdom
import { api } from "@/api/client";
import PromptStudioView from "@/features/skills/PromptStudioView";
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor,within } from "@testing-library/react";
import { afterEach,expect,it,vi } from "vitest";

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
            module: "team_skills.evolution.stages.summarize",
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
            module: "team_skills.evolution.stages.judge",
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
          evolution_max_sessions: 100,
          min_group_sessions: 2,
          min_group_users: 2,
          evolution_trigger_mode: "manual",
          evolution_interval_seconds: 600,
          bundle_edit_max_bytes: 262144,
          bundle_prompt_max_bytes: 786432,
          bundle_allow_delete: true,
        },
        validation: {
          auto_evaluate: true,
          publish_mode: "manual",
        },
      };
    }
    if (path === "/api/skill-evolution/trigger") {
      return { job_id: "evolve:test", status_url: "/api/jobs/evolve:test" };
    }
    if (path === "/api/skill-evolution/prompts/summarize") {
      return {
        id: "summarize",
        label: "会话总结 Summarize",
        description: "总结会话",
        module: "team_skills.evolution.stages.summarize",
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
        module: "team_skills.evolution.stages.judge",
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
  fireEvent.click(screen.getByRole("button", { name: "立即进化" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/skill-evolution/trigger",
    expect.objectContaining({ method: "POST", body: "{}" }),
  ));
  await waitFor(() => {
    expect(api).toHaveBeenCalledWith("/api/skill-evolution/prompts/judge");
  });
});

it("shows concurrent evidence branches and a required validation join", async () => {
  const stage = (id: string, label: string, extra = {}) => ({
    id, label, kind: "llm", prompt_id: id, description: `${label}说明`, ...extra,
  });
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/skill-evolution/pipeline") {
      return {
        nodes: [
          { id: "group", label: "按技能分组", kind: "logic" },
          stage("evolve_skill", "改进技能", {
            conditional: true, parallel_group: "evidence_generation", branch: "skill",
          }),
          stage("create_skill", "新建技能", {
            conditional: true, parallel_group: "evidence_generation", branch: "skill",
          }),
          stage("merge", "冲突合并", { conditional: true }),
          stage("dataset_synthesis", "测试集生成", {
            parallel_group: "evidence_generation", branch: "dataset",
          }),
          { id: "validate", label: "真回放校验", kind: "gate", join: "all" },
        ],
        edges: [
          { from: "group", to: "evolve_skill" },
          { from: "group", to: "create_skill" },
          { from: "group", to: "dataset_synthesis" },
          { from: "evolve_skill", to: "merge" },
          { from: "create_skill", to: "merge" },
          ...["evolve_skill", "create_skill", "merge", "dataset_synthesis"].map((from) => ({
            from, to: "validate",
          })),
        ],
      };
    }
    if (path === "/api/skill-evolution/prompts") return { prompts: [] };
    if (path === "/api/skill-evolution/session-samples?limit=50") return { sessions: [] };
    if (path === "/api/skill-evolution/settings") return {};
    if (path === "/api/skill-evolution/prompts/dataset_synthesis") {
      return {
        id: "dataset_synthesis", label: "测试集生成", default_prompt: "Evidence only",
        effective_prompt: "Evidence only", temperature: 0.3, max_tokens: 16384,
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });

  render(<PromptStudioView active user={{ id: "admin", role: "admin" }} />);
  const parallel = await screen.findByRole("group", { name: "技能进化与测试集并行" });
  expect(within(parallel).getByRole("button", { name: /改进技能/ })).toBeVisible();
  expect(within(parallel).getByRole("button", { name: /新建技能/ })).toBeVisible();
  const dataset = within(parallel).getByRole("button", { name: /测试集生成/ });
  expect(within(dataset).queryByText("条件")).not.toBeInTheDocument();
  expect(within(screen.getByRole("button", { name: /冲突合并/ })).getByText("条件")).toBeVisible();
  const validation = screen.getByRole("button", { name: /真回放校验/ });
  expect(within(validation).getByText("汇合")).toBeVisible();
  expect(within(validation).queryByText("条件")).not.toBeInTheDocument();
  expect(screen.queryByText("并行分支")).not.toBeInTheDocument();
  fireEvent.click(dataset);
  expect(await screen.findByDisplayValue("Evidence only")).toBeVisible();
});
