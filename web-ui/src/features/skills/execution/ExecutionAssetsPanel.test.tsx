// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import type { ExecutionPlan, ExecutionProfile } from "@/features/skills/api/client";
import ExecutionAssetsPanel from "./ExecutionAssetsPanel";
import { executionProfilesUrl, profileUrl, releaseUrl, type ExecutionReleaseDetail } from "./types";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
beforeEach(() => window.history.replaceState(null, "", "/console?tenant=test&view=dashboard&tab=prompts"));

const baseline: ExecutionPlan = {
  components: [
    { component_id: "customer-system", kind: "agent_prompt", slot: "system", order: 0, revision: 1,
      content_hash: "prompt-old-hash", content: "原始客户指令\n自然交流。" },
    { component_id: "customer-context", kind: "context_fragment", slot: "context", order: 1, revision: 1,
      content_hash: "context-hash", content: "这是供 Agent 注入的参考内容。" },
  ],
};
const candidate: ExecutionPlan = { components: [
  { ...baseline.components[0], revision: 2, content_hash: "prompt-new-hash", content: "演练客户：\n自然交流。\n拒绝收集手机号。" },
  baseline.components[1],
] };
const profile: ExecutionProfile = {
  profile_id: "customer:one", agent_id: "agent-one", user_id: "alice", runtime_type: "observed_http",
  affected_skill_ids: [], environment: {}, revision: 3, current_release_id: "release:new", composition_hash: "composition-new",
  execution_plan: candidate, releases: [
    { release_id: "release:new", composition_hash: "composition-new", candidate_id: "candidate-one" },
    { release_id: "release:old", composition_hash: "composition-old" },
  ], receipts: [
    { release_id: "release:new", composition_hash: "composition-new", state: "activated", request_id: "receipt-one", evidence_level: "agent_reported" },
    { release_id: "release:old", composition_hash: "composition-old", state: "downloaded", request_id: "receipt-old", evidence_level: "agent_reported" },
  ],
};
const newRelease: ExecutionReleaseDetail = {
  profile_id: profile.profile_id, current_release_id: profile.current_release_id,
  release: { release_id: "release:new", composition_hash: "composition-new", candidate_id: "candidate-one", candidate_revision: 2,
    validation_ref: "command-publish", document: { execution_plan: candidate, skill_dependencies: [], origin: "candidate-review" } },
  validation: { status: "available", run_id: "validation-exact-one", candidate_id: "candidate-one", candidate_revision: 2,
    baseline_execution_plan: baseline, evaluation_hash: "frozen-eval-one", evaluation: {
      validation_verdict: "accept", execution_gate: { status: "passed" },
      // Before independent review this old wrapper verdict is not the underlying Replay result.
      replay: { verdict: "inconclusive", checklist: {
        baseline: { reports: [{ all_satisfied: false }, { all_satisfied: false }] },
        candidate: { reports: [{ all_satisfied: true }, { all_satisfied: true }] },
      }, efficiency: { dimensions: {
        interaction_turns: { baseline: 4, candidate: 2, delta: 2, reduction_ratio: 0.5, winner: "candidate" },
        tool_call_count: { baseline: 0, candidate: 0, delta: 0, reduction_ratio: 0, winner: "tie" },
        total_tokens: { baseline: 1200, candidate: 1250, delta: -50, reduction_ratio: -0.04, winner: "baseline" },
      } } },
    } },
};
const oldRelease: ExecutionReleaseDetail = {
  profile_id: profile.profile_id, current_release_id: profile.current_release_id,
  release: { release_id: "release:old", composition_hash: "composition-old",
    document: { execution_plan: baseline, skill_dependencies: [], origin: "baseline-registration" } },
  validation: { status: "not_applicable", reason: "baseline_registration", evaluation: null },
};

function installApi(overrides: Partial<Record<string, unknown>> = {}) {
  const responses: Record<string, unknown> = {
    [executionProfilesUrl]: { profiles: [profile] }, [profileUrl(profile.profile_id)]: profile,
    [releaseUrl(profile.profile_id, "release:new")]: newRelease,
    [releaseUrl(profile.profile_id, "release:old")]: oldRelease,
    ...overrides,
  };
  vi.mocked(api).mockImplementation(async path => {
    const response = responses[path];
    if (response instanceof Error) throw response;
    if (response === undefined) throw new Error(`unexpected request: ${path}`);
    return response as any;
  });
}

const bodyText = (value: string) => value.replace(/\s+/g, " ").trim();

async function openVersion() {
  fireEvent.click(await screen.findByRole("button", { name: `查看版本 · ${profile.profile_id}` }));
  return screen.findByRole("dialog");
}

describe("ExecutionAssetsPanel", () => {
  it("loads only while active and exposes actual component versions, search and the registration entry", async () => {
    installApi();
    const onManage = vi.fn();
    const view = render(<ExecutionAssetsPanel active={false} onManage={onManage} />);
    expect(api).not.toHaveBeenCalled();
    view.rerender(<ExecutionAssetsPanel active onManage={onManage} />);
    expect(await screen.findByText("Prompt · customer-system · v2")).toBeVisible();
    expect(screen.getByText("上下文 · customer-context · v1")).toBeVisible();
    expect(screen.getByText("TE 当前发布：release:new")).toBeVisible();
    expect(screen.getByText(/关联 Skill：无/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("查找执行配置或 Agent"), { target: { value: "agent-missing" } });
    expect(screen.getByText("没有匹配的执行配置。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "登记与进化设置" }));
    expect(onManage).toHaveBeenCalledOnce();
    expect(vi.mocked(api).mock.calls).toHaveLength(1);
  });

  it("shows exact release text, context, frozen comparison and release-bound Replay metrics without model calls", async () => {
    installApi();
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    expect(await within(dialog).findByText(bodyText(candidate.components[0].content))).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText("业务组件"), { target: { value: "customer-context" } });
    expect(within(dialog).getByText(baseline.components[1].content)).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText("业务组件"), { target: { value: "customer-system" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "版本差异" }));
    expect(within(dialog).getByText("原始客户指令")).toBeVisible();
    expect(within(dialog).getByText("拒绝收集手机号。")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getAllByText("通过")).toHaveLength(1);
    expect(within(dialog).getByText("2/2 个 Case 全部满足")).toBeVisible();
    expect(within(dialog).getByText("0/2 个 Case 全部满足")).toBeVisible();
    expect(within(dialog).getByText("增加 50")).toBeVisible();
    expect(within(dialog).getByText(/validation-exact-one/)).toBeVisible();
    expect(vi.mocked(api).mock.calls.every(([path, options]) => path.startsWith(executionProfilesUrl) && !options)).toBe(true);
    expect(vi.mocked(api).mock.calls).toHaveLength(3);
  });

  it("does not reuse new-version metrics for the baseline and filters Agent reports by release", async () => {
    installApi();
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    await within(dialog).findByText(bodyText(candidate.components[0].content));
    fireEvent.click(within(dialog).getByRole("button", { name: "发布与 Agent 回执" }));
    expect(within(dialog).getByText(/Agent 自报已激活/)).toBeVisible();
    expect(within(dialog).queryByText(/Agent 自报已下载/)).not.toBeInTheDocument();
    expect(within(dialog).getByText(/不代表当前仍在使用/)).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText("查看发布版本"), { target: { value: "release:old" } });
    expect(await within(dialog).findByText(/Agent 自报已下载/)).toBeVisible();
    expect(within(dialog).queryByText(/Agent 自报已激活/)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getByText(/初始登记基线，没有新旧版本对比指标/)).toBeVisible();
    expect(within(dialog).queryByRole("table", { name: "A/B 效率对比" })).not.toBeInTheDocument();
    expect(new URL(window.location.href).searchParams.get("execution_release")).toBe("release:old");
  });

  it("shows the release-bound external scores and thresholds without carrying them into another version", async () => {
    const scored: ExecutionReleaseDetail = structuredClone(newRelease);
    scored.validation.evaluation!.replay = {
      ...scored.validation.evaluation!.replay,
      evaluation_policy_hash: "frozen-evaluator-one",
      decision_policy: { policy: "external-agent-score", verdict: "accept", decision_basis: "external_score_improved" },
      external_evaluation: { policy: "external-agent-score", baseline_score: 65, candidate_score: 85,
        score_delta: 20, case_count: 2, evaluated_case_count: 2, min_score: 80, min_improvement: 10,
        decision_basis: "external_score_improved" },
    };
    installApi({ [releaseUrl(profile.profile_id, "release:new")]: scored });
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    await within(dialog).findByText(bodyText(candidate.components[0].content));
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getByRole("table", { name: "A/B 外部评分对比" })).toBeVisible();
    expect(within(dialog).getByText("85")).toBeVisible();
    expect(within(dialog).getByText(/本次冻结门槛：候选平均分至少 80；提升至少 10/)).toBeVisible();
    expect(within(dialog).getByText("评价配置指纹：frozen-evaluator-one")).toBeVisible();
    expect(within(dialog).getByRole("table", { name: "A/B 效率对比" })).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText("查看发布版本"), { target: { value: "release:old" } });
    await within(dialog).findByText(/初始登记基线，没有新旧版本对比指标/);
    expect(within(dialog).queryByRole("table", { name: "A/B 外部评分对比" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/frozen-evaluator-one/)).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.every(([path, options]) => path.startsWith(executionProfilesUrl) && !options)).toBe(true);
  });

  it("supports a read-only deep link and preserves tenant, view and tab when the dialog closes", async () => {
    window.history.replaceState(null, "", "/console?tenant=test&view=dashboard&tab=prompts&execution_profile=customer%3Aone&execution_release=release%3Aold");
    installApi();
    render(<ExecutionAssetsPanel active />);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(bodyText(baseline.components[0].content))).toBeVisible();
    expect(api).toHaveBeenCalledWith(releaseUrl(profile.profile_id, "release:old"));
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭版本详情" }));
    const params = new URL(window.location.href).searchParams;
    expect(params.get("tenant")).toBe("test");
    expect(params.get("view")).toBe("dashboard");
    expect(params.get("tab")).toBe("prompts");
    expect(params.has("execution_profile")).toBe(false);
    expect(params.has("execution_release")).toBe(false);
  });

  it("rejects a deep link to an unknown profile without opening another profile", async () => {
    window.history.replaceState(null, "", "/console?tenant=test&execution_profile=missing");
    installApi();
    render(<ExecutionAssetsPanel active />);
    expect(await screen.findByRole("alert")).toHaveTextContent("未找到执行配置：missing");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls).toHaveLength(1);
  });

  it("ignores a late release response after the user switches to an older release", async () => {
    let resolveNew!: (value: ExecutionReleaseDetail) => void;
    const pendingNew = new Promise<ExecutionReleaseDetail>(resolve => { resolveNew = resolve; });
    installApi();
    const standard = vi.mocked(api).getMockImplementation()!;
    vi.mocked(api).mockImplementation(path => path === releaseUrl(profile.profile_id, "release:new") ? pendingNew : standard(path));
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    await waitFor(() => expect(within(dialog).getByLabelText("查看发布版本")).toContainHTML("release:old"));
    fireEvent.change(within(dialog).getByLabelText("查看发布版本"), { target: { value: "release:old" } });
    expect(await within(dialog).findByText(bodyText(baseline.components[0].content))).toBeVisible();
    await act(async () => { resolveNew(newRelease); await pendingNew; });
    expect(within(dialog).getByText(bodyText(baseline.components[0].content))).toBeVisible();
    expect(within(dialog).queryByText(bodyText(candidate.components[0].content))).not.toBeInTheDocument();
  });

  it("shows a retryable list error and a useful empty state", async () => {
    installApi({ [executionProfilesUrl]: new Error("连接暂不可用") });
    const onManage = vi.fn();
    render(<ExecutionAssetsPanel active onManage={onManage} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("连接暂不可用");
    installApi({ [executionProfilesUrl]: { profiles: [] } });
    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    expect(await screen.findByText("尚未登记业务 Prompt。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "登记业务 Prompt" }));
    expect(onManage).toHaveBeenCalledOnce();
  });

  it("keeps immutable text available when historical verification cannot be matched", async () => {
    installApi({ [releaseUrl(profile.profile_id, "release:new")]: {
      ...newRelease, validation: { status: "unavailable", reason: "validation_release_mismatch", evaluation: null },
    } });
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    expect(await within(dialog).findByText(bodyText(candidate.components[0].content))).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getByText(/历史发布缺少可核对的冻结验证记录/)).toBeVisible();
    expect(within(dialog).queryByRole("table")).not.toBeInTheDocument();
  });

  it("allows historical text comparison while keeping metrics fixed to the chosen release", async () => {
    installApi();
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    await within(dialog).findByText(bodyText(candidate.components[0].content));
    fireEvent.click(within(dialog).getByRole("button", { name: "版本差异" }));
    fireEvent.change(within(dialog).getByLabelText("正文对照来源"), { target: { value: "release:old" } });
    expect(await within(dialog).findByText("原始客户指令")).toBeVisible();
    expect(api).toHaveBeenCalledWith(releaseUrl(profile.profile_id, "release:old"));
    expect(within(dialog).getByLabelText("查看发布版本")).toHaveValue("release:new");
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getByText(/validation-exact-one/)).toBeVisible();
    expect(within(dialog).getByText("2/2 个 Case 全部满足")).toBeVisible();
  });

  it("lets users retry a failed release request without publishing or evaluating", async () => {
    installApi({ [releaseUrl(profile.profile_id, "release:new")]: new Error("发布详情暂不可用") });
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("发布详情暂不可用");
    installApi();
    fireEvent.click(within(dialog).getByRole("button", { name: "重试版本详情" }));
    expect(await within(dialog).findByText(bodyText(candidate.components[0].content))).toBeVisible();
    expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true);
  });

  it("keeps case pass counts distinct from checklist item counts and does not invent missing counts", async () => {
    installApi({ [releaseUrl(profile.profile_id, "release:new")]: {
      ...newRelease, validation: { ...newRelease.validation, evaluation: {
        ...newRelease.validation.evaluation,
        replay: { ...newRelease.validation.evaluation?.replay, checklist: {
          baseline: { case_count: 6, completed_count: 0, satisfied_count: 17, total: 32 },
          candidate: { case_count: 6, completed_count: null, satisfied_count: 32, total: 32 },
        } },
      } },
    } });
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    await within(dialog).findByText(bodyText(candidate.components[0].content));
    fireEvent.click(within(dialog).getByRole("button", { name: "Replay 指标" }));
    expect(within(dialog).getByText("0/6 个 Case 全部满足")).toBeVisible();
    expect(within(dialog).getByText("未知/6 个 Case 全部满足")).toBeVisible();
    expect(within(dialog).getByText("17/32 项满足")).toBeVisible();
    expect(within(dialog).getByText("32/32 项满足")).toBeVisible();
  });

  it("does not disguise a failed receipt request as an empty receipt list when text loading succeeds", async () => {
    installApi({ [profileUrl(profile.profile_id)]: new Error("回执读取超时") });
    render(<ExecutionAssetsPanel active />);
    const dialog = await openVersion();
    expect(await within(dialog).findByText(bodyText(candidate.components[0].content))).toBeVisible();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("回执读取超时");
    fireEvent.click(within(dialog).getByRole("button", { name: "发布与 Agent 回执" }));
    expect(within(dialog).getByText(/回执加载失败，当前无法判断/)).toBeVisible();
    expect(within(dialog).queryByText(/没有这个版本的下载或激活记录/)).not.toBeInTheDocument();
    installApi();
    fireEvent.click(within(dialog).getByRole("button", { name: "重试列表与回执" }));
    expect(await within(dialog).findByText(/Agent 自报已激活/)).toBeVisible();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });
});
