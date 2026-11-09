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
  expect(screen.getByText(/选中业务 Prompt 时，沿用同一条候选生成与验证路径/)).toBeVisible();
  expect(screen.getByText(/仅修改业务组件时，Skill 内容与版本保持不变/)).toBeVisible();
  expect(screen.queryByText("链路加载中…")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "立即进化" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/skill-evolution/trigger",
    expect.objectContaining({ method: "POST", body: JSON.stringify({ allowed_modules: ["skill"], execution_profile_id: "" }) }),
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
  const parallel = await screen.findByRole("group", { name: "候选生成与测试集并行" });
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

function mockScopeSettings() {
  const settings = {
    revision: 8,
    evolve: { evolution_allowed_modules: ["skill"] },
    validation: { auto_evaluate: true, publish_mode: "manual" },
  };
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === "/api/skill-evolution/execution-profiles") return { profiles: [{
      profile_id: "profile-one", agent_id: "agent-one", runtime_type: "agentshub", user_id: "alice",
      revision: 1, affected_skill_ids: ["demo"], composition_hash: "baseline-hash",
      execution_plan: { components: [] },
    }] };
    if (path === "/api/skill-evolution/pipeline") return { nodes: [], edges: [] };
    if (path === "/api/skill-evolution/prompts") return { prompts: [] };
    if (path.startsWith("/api/skill-evolution/session-samples")) return { sessions: [] };
    if (path === "/api/skill-evolution/settings") {
      if (options?.method === "PUT") {
        const body = JSON.parse(options.body as string);
        return { ...settings, revision: 9, evolve: { ...settings.evolve, ...body.evolve } };
      }
      return settings;
    }
    if (path === "/api/skill-evolution/trigger") return { job_id: "evolve:scope" };
    throw new Error(`unexpected request: ${path}`);
  });
}

it("sends the temporary module selection without saving the plan and prevents an empty scope", async () => {
  mockScopeSettings();
  render(<PromptStudioView active user={{ id: "admin", role: "admin" }} />);
  const scope = await screen.findByRole("group", { name: "本次允许修改的模块" });
  await waitFor(() => expect(screen.getByRole("button", { name: "立即进化" })).toBeEnabled());
  fireEvent.click(within(scope).getByRole("checkbox", { name: "业务 Agent Prompt／上下文注入" }));
  fireEvent.click(within(scope).getByRole("checkbox", { name: "Skill" }));
  expect(screen.getByRole("button", { name: "立即进化" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("本次业务 Agent 执行配置"), { target: { value: "profile-one" } });
  fireEvent.click(screen.getByRole("button", { name: "立即进化" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/skill-evolution/trigger",
    expect.objectContaining({ body: JSON.stringify({ allowed_modules: ["prompt"], execution_profile_id: "profile-one" }) }),
  ));
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(0);
  await waitFor(() => expect(screen.getByRole("button", { name: "立即进化" })).toBeEnabled());
  fireEvent.click(within(scope).getByRole("checkbox", { name: "业务 Agent Prompt／上下文注入" }));
  expect(screen.getByRole("button", { name: "立即进化" })).toBeDisabled();
  expect(within(scope).getByRole("alert")).toHaveTextContent("请至少选择一个");
});

it("saves the plan scope explicitly with its revision while retaining the temporary selection", async () => {
  mockScopeSettings();
  render(<PromptStudioView active user={{ id: "admin", role: "admin" }} />);
  await screen.findByText("默认修改范围（含自动计划）");
  fireEvent.click(screen.getByText("默认修改范围（含自动计划）"));
  const plan = screen.getByRole("group", { name: "后续任务默认允许修改的模块" });
  fireEvent.click(within(plan).getByRole("checkbox", { name: "业务 Agent Prompt／上下文注入" }));
  fireEvent.click(screen.getByRole("button", { name: "保存默认修改范围" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/skill-evolution/settings",
    expect.objectContaining({ body: JSON.stringify({
      revision: 8, evolve: { evolution_allowed_modules: ["skill", "prompt"], evolution_execution_profile_id: "" },
    }) }),
  ));
  const temporary = screen.getByRole("group", { name: "本次允许修改的模块" });
  expect(within(temporary).getByRole("checkbox", { name: "业务 Agent Prompt／上下文注入" })).not.toBeChecked();
  expect(screen.getByText("TE 进化引擎 Prompt")).toBeVisible();
});
