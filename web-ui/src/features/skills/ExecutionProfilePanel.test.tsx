// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import ExecutionProfilePanel from "@/features/skills/ExecutionProfilePanel";
import { toastErr } from "@/lib/toast";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const registrationPreview = { profile_id: "agent-baseline", preview_hash: "preview-hash", summary: { component_count: 2, affected_skill_count: 2 } };

const profile = {
  profile_id: "agent-baseline", agent_id: "agent-one", user_id: "alice", runtime_type: "agentshub",
  revision: 1, affected_skill_ids: ["report", "review"], composition_hash: "baseline-hash",
  environment: { request: { model: "business-model", temperature: 0 } },
  execution_plan: { components: [{
    component_id: "agent-prompt", kind: "agent_prompt", slot: "system", order: 0,
    content: "Existing business instructions", revision: 1, content_hash: "prompt-hash",
  }] },
};

function fillBaseline(environment = "") {
  fireEvent.click(screen.getByText("登记现有业务 Agent 执行基线"));
  for (const [label, value] of [
    ["执行配置 ID", "agent-baseline"], ["业务 Agent ID", "agent-one"], ["执行用户 ID", "alice"],
    ["全部受影响的 Skill 名称（逗号或换行分隔）", "report,review"],
    ["当前业务 Agent Prompt", "Existing business instructions"],
    ["当前受管理的业务上下文注入（可选）", "Existing context"],
    ["冻结执行环境 JSON（可选）", environment],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("ExecutionProfilePanel", () => {
  it("requires a real business Prompt when onboarding context components", () => {
    vi.mocked(api).mockResolvedValue({ profiles: [] });
    render(<ExecutionProfilePanel active isAdmin profileId="" onChange={vi.fn()} />);
    fillBaseline();
    fireEvent.change(screen.getByLabelText("当前业务 Agent Prompt"), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "预览业务执行基线" })).toBeDisabled();
    expect(screen.getByText(/上下文组件需要关联真实业务 Prompt/)).toBeVisible();
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("registers a real Prompt baseline with zero Skill dependencies and no placeholder Skill", async () => {
    const pure = { ...profile, affected_skill_ids: [], skill_dependencies: [], current_release_id: "prompt-release-1" };
    vi.mocked(api).mockImplementation(async (path, options) => path.endsWith("/preview") ? registrationPreview : options?.method === "POST" ? pure : { profiles: [pure] });
    const change = vi.fn();
    const view = render(<ExecutionProfilePanel active isAdmin profileId="" onChange={change} />);
    fillBaseline(JSON.stringify({ request: { model: "business-model", temperature: 0 } }));
    fireEvent.change(screen.getByLabelText("全部受影响的 Skill 名称（逗号或换行分隔）"), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "预览业务执行基线" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
    await screen.findByRole("button", { name: "登记业务执行基线" });
    fireEvent.click(screen.getByRole("button", { name: "登记业务执行基线" }));
    await waitFor(() => expect(change).toHaveBeenCalledWith(pure.profile_id));
    const post = vi.mocked(api).mock.calls.find(([path, options]) => options?.method === "POST" && !path.endsWith("/preview"));
    expect(JSON.parse(post?.[1]?.body as string).affected_skill_ids).toEqual([]);
    expect(vi.mocked(api).mock.calls.some(([path]) => path === "/api/skills")).toBe(false);
    view.rerender(<ExecutionProfilePanel active isAdmin profileId={pure.profile_id} onChange={change} />);
    fireEvent.click(screen.getByText("查看当前发布内容"));
    expect(screen.getByText("关联 Skill：无（纯 Prompt 基线）")).toBeVisible();
    expect(screen.getByText("Skill 依赖：0 个")).toBeVisible();
    expect(screen.getByText("当前组合发布：prompt-release-1")).toBeVisible();
  });

  it("previews a fixed business baseline without exposing an in-place edit action", async () => {
    vi.mocked(api).mockResolvedValue({ profiles: [profile] });
    const change = vi.fn();
    render(<ExecutionProfilePanel active isAdmin={false} profileId={profile.profile_id} onChange={change} />);
    fireEvent.click(await screen.findByText("查看当前发布内容"));
    expect(screen.getByText("Existing business instructions")).toBeVisible();
    expect(screen.getByText("关联 Skill：report、review")).toBeVisible();
    expect(screen.getByText(/business-model/)).toBeVisible();
    expect(screen.queryByLabelText("当前业务 Agent Prompt")).not.toBeInTheDocument();
    expect(screen.queryByText("登记现有业务 Agent 执行基线")).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  it("registers the actual business Agent baseline and selects it without saving evolution settings", async () => {
    vi.mocked(api).mockImplementation(async (path, options) => path.endsWith("/preview") ? registrationPreview : options?.method === "POST" ? profile : { profiles: [profile] });
    const change = vi.fn();
    render(<ExecutionProfilePanel active isAdmin profileId="" onChange={change} />);
    const environment = {
      request: { model: "business-model", tools: [], temperature: 0, max_tokens: 2000 },
      template_variables: { business: "training" },
      validation: { holdout_dataset_ids: ["blind-set"], repetitions: 2, max_total_tokens: 1000000 },
    };
    fillBaseline(JSON.stringify(environment));
    fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
    await screen.findByRole("button", { name: "登记业务执行基线" });
    fireEvent.click(screen.getByRole("button", { name: "登记业务执行基线" }));
    await waitFor(() => expect(change).toHaveBeenCalledWith("agent-baseline"));
    const post = vi.mocked(api).mock.calls.find(([path, options]) => options?.method === "POST" && !path.endsWith("/preview"));
    expect(post?.[0]).toBe("/api/skill-evolution/execution-profiles");
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({
      profile_id: "agent-baseline", runtime_type: "agentshub", agent_id: "agent-one", user_id: "alice",
      affected_skill_ids: ["report", "review"], environment, components: [
        { component_id: "agent-baseline:agent-prompt", kind: "agent_prompt", slot: "system", order: 0, content: "Existing business instructions" },
        { component_id: "agent-baseline:business-context", kind: "context_fragment", slot: "context", order: 1, content: "Existing context" },
      ],
    });
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith("/settings"))).toBe(false);
  });

  it("registers variable contexts from the selected release without changing the source configuration", async () => {
    const declared = { ...profile, current_release_id: "source-release", execution_plan: {
      ...profile.execution_plan,
      environment: { template_input_contract: { schema_version: "teamevolver.template-input-contract.v1", variable_names: ["rules"] } },
    } };
    const created = { ...declared, profile_id: "context-baseline", current_release_id: "new-release" };
    const base = `/api/skill-evolution/execution-profiles/${profile.profile_id}/variable-contexts`;
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === `${base}/preview`) return {
        profile_id: created.profile_id, source_profile_id: declared.profile_id, source_release_id: "source-release",
        components: [], preview_hash: "server-preview", summary: { sample_count: 1, managed_variables: [], component_count: 2 },
      };
      if (path === base && options?.method === "POST") return created;
      return { profiles: [declared] };
    });
    const change = vi.fn();
    render(<ExecutionProfilePanel active isAdmin profileId={profile.profile_id} onChange={change} />);
    fireEvent.click(await screen.findByRole("button", { name: "以此配置登记变量上下文" }));
    fireEvent.change(screen.getByLabelText("新执行配置 ID"), { target: { value: created.profile_id } });
    fireEvent.change(screen.getByLabelText("变量值提供方式"), { target: { value: "variable_samples" } });
    fireEvent.change(screen.getByLabelText("变量样本 JSON 数组"), { target: { value: '[{"rules":"original rule"}]' } });
    fireEvent.click(screen.getByRole("checkbox", { name: "rules" }));
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认登记新基线" }));
    await waitFor(() => expect(change).toHaveBeenCalledWith(created.profile_id));
    expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method === "POST").map(([path]) => path)).toEqual([`${base}/preview`, base]);
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
    expect(screen.queryByRole("region", { name: "登记变量上下文基线" })).not.toBeInTheDocument();
  });

  it("allows registration with an empty environment and explains that execution still needs configuration", async () => {
    vi.mocked(api).mockImplementation(async (path, options) => path.endsWith("/preview") ? registrationPreview : options?.method === "POST" ? profile : { profiles: [profile] });
    render(<ExecutionProfilePanel active isAdmin profileId="" onChange={vi.fn()} />);
    fillBaseline();
    expect(screen.getByText(/留空仅登记基线/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
    await screen.findByRole("button", { name: "登记业务执行基线" });
    fireEvent.click(screen.getByRole("button", { name: "登记业务执行基线" }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
    const post = vi.mocked(api).mock.calls.find(([path, options]) => options?.method === "POST" && !path.endsWith("/preview"));
    expect(JSON.parse(post?.[1]?.body as string).environment).toEqual({});
  });

  it.each(["{broken", "null", "[]", '{"request":{"messages":[]}}', '{"request":{"api_key":"placeholder"}}'])(
    "rejects invalid or sensitive environment content before registration: %s", async value => {
      vi.mocked(api).mockResolvedValue({ profiles: [] });
      render(<ExecutionProfilePanel active isAdmin profileId="" onChange={vi.fn()} />);
      fillBaseline(value);
      fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
      await waitFor(() => expect(toastErr).toHaveBeenCalled());
      expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    },
  );

  it("discovers template placeholders, requires explicit defaults and previews before registration", async () => {
    vi.mocked(api).mockImplementation(async (path, options) => path.endsWith("/preview") ? registrationPreview
      : options?.method === "POST" ? profile : { profiles: [] });
    render(<ExecutionProfilePanel active isAdmin profileId="" onChange={vi.fn()} />);
    fillBaseline('{"request":{"temperature":0.25,"tools":[]}}');
    fireEvent.change(screen.getByLabelText("当前业务 Agent Prompt"), { target: { value: "Apply {{ rules }} to {{chatHistory}}" } });
    fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("rules、chatHistory");
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    fireEvent.change(screen.getByLabelText("rules 原始默认值"), { target: { value: "Exact original rule\n" } });
    fireEvent.click(screen.getByRole("button", { name: "为 chatHistory 明确使用空值" }));
    fireEvent.change(screen.getByLabelText("业务 Agent 模型（可选）"), { target: { value: "actual-agent-model" } });
    fireEvent.click(screen.getByRole("button", { name: "预览业务执行基线" }));
    await screen.findByRole("button", { name: "登记业务执行基线" });
    const posted = vi.mocked(api).mock.calls.find(([path]) => path.endsWith("/preview"));
    expect(JSON.parse(posted![1]!.body as string).environment).toEqual({
      request: { temperature: 0.25, tools: [], model: "actual-agent-model" },
      template_variables: { rules: "Exact original rule\n", chatHistory: "" },
      template_input_contract: { schema_version: "teamevolver.template-input-contract.v1", variable_names: ["rules", "chatHistory"] },
    });
    expect(vi.mocked(api).mock.calls.some(([path, options]) => path === "/api/skill-evolution/execution-profiles" && options?.method === "POST")).toBe(false);
    fireEvent.change(screen.getByLabelText("当前业务 Agent Prompt"), { target: { value: "Changed {{rules}}" } });
    expect(screen.queryByRole("button", { name: "登记业务执行基线" })).not.toBeInTheDocument();
  });
});
