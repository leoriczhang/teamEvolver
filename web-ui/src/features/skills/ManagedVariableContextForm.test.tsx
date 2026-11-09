// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import type { ExecutionProfile, VariableContextRegistrationPreview } from "@/features/skills/api/client";
import ManagedVariableContextForm from "./ManagedVariableContextForm";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastOk: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const source: ExecutionProfile = {
  profile_id: "source:one", runtime_type: "observed_http", agent_id: "agent-one", user_id: "alice",
  affected_skill_ids: [], revision: 3, current_release_id: "source-release", composition_hash: "source-composition",
  environment: { request: { model: "private-model", credential_ref: "secret://not-shown" } },
  execution_plan: {
    environment: { template_input_contract: { schema_version: "teamevolver.template-input-contract.v1", variable_names: ["rules", "example"] } },
    components: [{ component_id: "source-prompt", kind: "agent_prompt", slot: "system", order: 0,
      content: "Rules: {{rules}}\nExample: {{example}}", revision: "1", content_hash: "template-hash" }],
  },
};
const literal = "  Preserve {{literal}}\n\"quoted\"  ";
const samples = [{ rules: literal, example: "reference" }, { rules: literal }, { rules: " \n " }];
const preview: VariableContextRegistrationPreview = {
  profile_id: "new:one", source_profile_id: source.profile_id, source_release_id: source.current_release_id,
  components: [{ ...source.execution_plan.components[0], component_id: "new:one:prompt" }, {
    component_id: "server-stable-asset", kind: "context_fragment", slot: "context", order: 1,
    content: literal, revision: "1", content_hash: "asset-hash",
    binding: { inline_variable: { component_id: "new:one:prompt", name: "rules", source_value_hash: "original-value-hash" } },
  }],
  summary: { sample_count: 3, managed_variables: [{ name: "rules", unique_values: 1, blank_samples: 1, component_ids: ["server-stable-asset"] }], component_count: 2 },
  preview_hash: "server-preview-hash",
};
const created: ExecutionProfile = { ...source, profile_id: "new:one", current_release_id: "new-release", execution_plan: { components: preview.components as ExecutionProfile["execution_plan"]["components"] } };
const endpoint = "/api/skill-evolution/execution-profiles/source%3Aone/variable-contexts";

function fill(rows: unknown = samples) {
  fireEvent.change(screen.getByLabelText("新执行配置 ID"), { target: { value: "new:one" } });
  fireEvent.change(screen.getByLabelText("变量值提供方式"), { target: { value: "variable_samples" } });
  fireEvent.change(screen.getByLabelText("变量样本 JSON 数组"), { target: { value: JSON.stringify(rows) } });
  fireEvent.click(screen.getByRole("checkbox", { name: "rules" }));
}

describe("ManagedVariableContextForm", () => {
  it("registers independent value collections for multiple variables without constructing their combinations", async () => {
    vi.mocked(api).mockResolvedValue({ ...preview, summary: { ...preview.summary, input_mode: "variable_values", sample_count: 5 } });
    render(<ManagedVariableContextForm source={source} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("新执行配置 ID"), { target: { value: "new:one" } });
    for (const name of ["rules", "example"]) fireEvent.click(screen.getByRole("checkbox", { name }));
    fireEvent.change(screen.getByLabelText("rules 取值 1"), { target: { value: "  Exact rule one\n" } });
    fireEvent.click(screen.getByRole("button", { name: "为 rules 添加取值" }));
    fireEvent.change(screen.getByLabelText("rules 取值 2"), { target: { value: "Rule two" } });
    for (const [index, value] of ["Example one", "Example two", "Example three"].entries()) {
      if (index) fireEvent.click(screen.getByRole("button", { name: "为 example 添加取值" }));
      fireEvent.change(screen.getByLabelText(`example 取值 ${index + 1}`), { target: { value } });
    }
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    expect(await screen.findByText("5 个变量取值 · 新组合共 2 个组件")).toBeVisible();
    expect(JSON.parse(vi.mocked(api).mock.calls[0][1]?.body as string)).toEqual({
      profile_id: "new:one", source_release_id: "source-release", managed_variables: ["rules", "example"],
      evolution_scope: "context", variable_values: { rules: ["  Exact rule one\n", "Rule two"], example: ["Example one", "Example two", "Example three"] },
    });
  });

  it("previews exact sample values and registers the same server-bound request without runtime secrets", async () => {
    vi.mocked(api).mockImplementation(async path => path.endsWith("/preview") ? preview : created);
    const registered = vi.fn();
    render(<ManagedVariableContextForm source={source} onRegistered={registered} onCancel={vi.fn()} />);
    expect(screen.getByRole("radio", { name: "仅上下文资产" })).toBeChecked();
    expect(screen.queryByText(/private-model|secret:\/\//)).not.toBeInTheDocument();
    fill();
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    const result = await screen.findByRole("region", { name: "变量上下文预览" });
    expect(within(result).getByText("3 条样本 · 新组合共 2 个组件")).toBeVisible();
    expect(within(result).getByText(/rules：1 个变量组，1 个不同值，1 条空值样本/)).toBeVisible();
    fireEvent.click(within(result).getByText("变量组 rules · 1 个取值 · 1 个注入成员"));
    fireEvent.click(within(result).getByText("rules · server-stable-asset · v1"));
    expect(within(result).getByText("注入位置：new:one:prompt 中的 {{rules}}")).toBeVisible();
    expect(within(result).getByText("原值哈希：original-value-hash")).toBeVisible();
    expect(result.querySelector("pre")?.textContent).toBe(literal);
    fireEvent.click(screen.getByRole("button", { name: "确认登记新基线" }));
    await waitFor(() => expect(registered).toHaveBeenCalledWith(created));
    const body = { profile_id: "new:one", source_release_id: "source-release", managed_variables: ["rules"], variable_samples: samples, evolution_scope: "context" };
    expect(vi.mocked(api).mock.calls.map(([path, options]) => [path, JSON.parse(options?.body as string)])).toEqual([
      [`${endpoint}/preview`, body], [endpoint, body],
    ]);
  });

  it("supports multiple managed variables and invalidates a preview when the evolution scope changes", async () => {
    vi.mocked(api).mockResolvedValue(preview);
    render(<ManagedVariableContextForm source={source} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    fill([{ rules: "rule", example: "example" }]);
    fireEvent.click(screen.getByRole("checkbox", { name: "example" }));
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    await screen.findByRole("button", { name: "确认登记新基线" });
    fireEvent.click(screen.getByRole("radio", { name: "Prompt 与上下文资产" }));
    expect(screen.queryByRole("button", { name: "确认登记新基线" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    await screen.findByRole("button", { name: "确认登记新基线" });
    expect(JSON.parse(vi.mocked(api).mock.calls[1][1]?.body as string)).toMatchObject({ managed_variables: ["rules", "example"], evolution_scope: "prompt_and_context" });
  });

  it.each([
    [null, "请提供 1–200"], [[], "请提供 1–200"], [[null], "必须是 JSON 对象"],
    [[{ example: "x" }], "缺少所选变量：rules"], [[{ rules: 7 }], "rules 必须是文本"],
    [[{ rules: "x", unknown: "x" }], "未声明的变量：unknown"],
    [[{ rules: "x", example: [] }], "example 必须是文本、数字或布尔值"],
    [Array.from({ length: 201 }, () => ({ rules: "x" })), "请提供 1–200"],
  ])("rejects invalid samples without a server request: %j", async (rows, message) => {
    render(<ManagedVariableContextForm source={source} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    fill(rows);
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(api).not.toHaveBeenCalled();
  });

  it.each([
    [{ ...preview, source_release_id: "stale-release" }, "预览与当前来源配置不一致"],
    [{ ...preview, summary: { ...preview.summary, component_count: 33 } }, "超过 32 个组件"],
  ])("rejects stale or oversized server previews", async (response, message) => {
    vi.mocked(api).mockResolvedValue(response);
    render(<ManagedVariableContextForm source={source} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("button", { name: "确认登记新基线" })).not.toBeInTheDocument();
  });

  it("discards a late preview when the source release changes", async () => {
    let resolve!: (value: VariableContextRegistrationPreview) => void;
    const pending = new Promise<VariableContextRegistrationPreview>(done => { resolve = done; });
    vi.mocked(api).mockReturnValue(pending);
    const view = render(<ManagedVariableContextForm source={source} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    view.rerender(<ManagedVariableContextForm source={{ ...source, current_release_id: "next-release" }} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    await act(async () => { resolve(preview); await pending; });
    expect(screen.queryByRole("region", { name: "变量上下文预览" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("新执行配置 ID")).toHaveValue("");
  });

  it("requires a new preview after a source-release conflict", async () => {
    vi.mocked(api).mockResolvedValueOnce(preview).mockRejectedValueOnce(Object.assign(new Error("conflict"), { status: 409 }));
    const registered = vi.fn();
    render(<ManagedVariableContextForm source={source} onRegistered={registered} onCancel={vi.fn()} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "预览变量上下文" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认登记新基线" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("来源发布或目标配置已发生变化");
    expect(screen.queryByRole("button", { name: "确认登记新基线" })).not.toBeInTheDocument();
    expect(registered).not.toHaveBeenCalled();
  });

  it("does not invent managed variables for profiles without a template input contract", () => {
    render(<ManagedVariableContextForm source={{ ...source, execution_plan: { components: source.execution_plan.components } }} onRegistered={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("此配置尚未声明模板变量");
    expect(screen.getByRole("button", { name: "预览变量上下文" })).toBeDisabled();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
