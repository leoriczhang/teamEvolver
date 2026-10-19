// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import MiningView from "./MiningView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
vi.mock("@/components/MarkdownWorkspace", () => ({ MarkdownDocument: ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock("@miner/MiningWhiteboxPanel", () => ({ default: () => null }));
vi.mock("@miner/SkillSourceWorkspace", () => ({ default: () => null }));

const mockedApi = vi.mocked(api);
const source = { source_id: "source-1", path: "viking://resources/mining/source", document_count: 2, total_bytes: 200, ready: true, tags: ["skill"] };
const artifact = { name: "SKILL.md", kind: "skill", path: "job-1/workspace/compiled_skill/example/SKILL.md", size_bytes: 50, skill_name: "example" };

function waitingJob(round = 1, maxRounds = 3, questions = true) {
  return {
    job_id: "job-1", name: "真实多轮挖掘", status: "waiting_human", input_dir: source.path,
    document_count: 2, current_round: round, max_rounds: maxRounds, rounds_mode: "orchestrated", engine: "ov_compile",
    runtime_timeout_seconds: 3600, human_checkpoints_supported: true,
    remote_warnings: [] as string[],
    phase: { step1: "done", step2: "done", step3: "done" },
    created_at: "2026-10-10T00:00:00Z", started_at: "2026-10-10T00:00:01Z", finished_at: "", updated_at: "2026-10-10T00:01:00Z",
    error: "", stop_reason: "", artifacts: [artifact],
    rounds: [{ round, artifacts: [artifact], ov_task_id: `compile-${round}`, remote_warnings: [] as string[] }],
    knowledge_supplements: [],
    pending_checkpoint: {
      id: `checkpoint-${round}`, round, checkpoint: "after_compile", title: "本轮 Evidence 复核", intro: "确认来源并补充缺口。",
      questions: questions ? [
        { qid: "q1", question: "计费除数的依据是什么？", severity: "高", answer_type: "long_text" },
        { qid: "q2", question: "是否适用特殊地区？", severity: "中", answer_type: "short_text" },
      ] : [],
      attachments: [],
    },
  };
}

type FixtureJob = Omit<ReturnType<typeof waitingJob>, "pending_checkpoint"> & {
  pending_checkpoint: ReturnType<typeof waitingJob>["pending_checkpoint"] | null;
};

function installApi(job: FixtureJob | null, onAction?: (path: string, body: Record<string, any>) => unknown) {
  let current = job;
  mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === "/api/mining/config") return {
      max_rounds_default: 3, max_rounds_range: [1, 5],
      runtime_timeout_seconds_default: 3600, runtime_timeout_seconds_range: [60, 7200],
    };
    if (path === "/api/mining/input-sources") return { sources: [source] };
    if (path === `/api/mining/input-sources/${source.source_id}`) return { source };
    if (path === "/api/mining/jobs" && !init?.method) return {
      jobs: current ? [current] : [], summary: { total: current ? 1 : 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3 },
    };
    if (path === "/api/mining/jobs/job-1" && !init?.method) return { job: current };
    if (path.startsWith("/api/mining/artifacts/content?")) {
      const artifactPath = decodeURIComponent(path.split("path=")[1]);
      return { path: artifactPath, name: artifactPath.split("/").pop(), content: "# Example", size_bytes: 9 };
    }
    if (init?.method === "POST") {
      const result = await onAction?.(path, JSON.parse(String(init.body)));
      if (result && typeof result === "object" && "job" in result) current = (result as { job: FixtureJob }).job;
      return result;
    }
    throw new Error(`Unexpected API call: ${path}`);
  });
}

async function openJob() {
  render(<MiningView active page="jobs" />);
  fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
  await screen.findByText("本轮 Evidence 复核");
}

async function openFailedJob() {
  render(<MiningView active page="jobs" />);
  fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
  return screen.findByRole("region", { name: "本轮 Compile 失败" });
}

describe("MiningView real mining rounds", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    vi.mocked(toastErr).mockClear();
  });
  afterEach(cleanup);

  it("shows actual whole-round Compile admissions separately from validated host subtasks", async () => {
    installApi({ ...waitingJob(1, 2, false), status: "running", pending_checkpoint: null, rounds: [],
      ...{ execution_protocol: "te-native-mining.v1", submission_seconds: 1,
        native_compile_history: [{ round: 1, task_id: "native-1" }], native_repairs: [] },
    });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    expect(await screen.findByText("整轮编译 · 累计已受理 1 次 Compile")).toBeInTheDocument();
    expect(screen.queryByText(/累计已校验.*个小任务/)).not.toBeInTheDocument();
  });

  it("explicitly retries a Native round once with the offered budget and labels the separate attempt", async () => {
    const context = { expected_task_id: "native-failed-1", budget_seconds: 1800, format_guidance_sha256: "a".repeat(64) };
    const initial = { ...waitingJob(2, 2, false), status: "failed", pending_checkpoint: null, error: "map: schema: invalid result",
      ...{ can_retry_native: true, native_retry_context: context },
    };
    let release!: (value: unknown) => void;
    const action = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    installApi(initial, action);
    await openFailedJob();
    const button = screen.getByRole("button", { name: "重新实测本轮" });
    expect(screen.getByText(/保留原任务和历史，新增一次 30 分钟运行期限/)).toHaveTextContent("不代表预计完成时间");
    expect(screen.queryByRole("button", { name: "从断点恢复" })).not.toBeInTheDocument();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(button).toBeDisabled();
    await waitFor(() => expect(action).toHaveBeenCalledTimes(1));
    expect(action).toHaveBeenCalledWith("/api/mining/jobs/job-1/resume", {
      action: "retry_with_new_budget", expected_task_id: context.expected_task_id, budget_seconds: 1800,
      request_id: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i),
    });
    release({ job: { ...initial, status: "preparing", error: "", can_retry_native: false, native_retry_context: null,
      native_explicit_retry: { attempt_budget: { seconds: 1800, started_at: "2026-10-10T00:02:00Z" } },
    } });
    expect(await screen.findByText("人工重试 · 本次运行期限：30 分 0 秒")).toBeInTheDocument();
    expect(screen.getByText("本轮经过时间（含中断等待）")).toBeInTheDocument();
    expect(screen.getByText("本次重试经过时间")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新实测本轮" })).not.toBeInTheDocument();
    await waitFor(() => expect(toastOk).toHaveBeenCalledWith("已重新实测本轮", expect.any(String)));
  });

  it("stops retry timing when the round reaches human review", async () => {
    installApi({ ...waitingJob(2, 2),
      ...{ execution_protocol: "te-native-mining.v1", round_completed_at: "2026-10-10T00:12:00Z",
        native_explicit_retry: { attempt_budget: { seconds: 3600, started_at: "2026-10-10T00:02:00Z" } } },
    });
    await openJob();
    const timer = screen.getByText("本次重试经过时间").parentElement!;
    expect(within(timer).getByText("10 分 0 秒")).toBeInTheDocument();
  });

  it.each([
    { can_retry_native: false, native_retry_context: { expected_task_id: "native-failed-1", budget_seconds: 1800, format_guidance_sha256: "a".repeat(64) } },
    { can_retry_native: true, native_retry_context: null },
    { can_retry_native: true, native_retry_context: { expected_task_id: "", budget_seconds: 1800, format_guidance_sha256: "a".repeat(64) } },
  ])("does not offer a Native retry without an eligible server context (%j)", async (eligibility) => {
    installApi({ ...waitingJob(2, 2, false), status: "failed", pending_checkpoint: null, error: "Compile failed", ...eligibility });
    await openFailedJob();
    expect(screen.queryByRole("button", { name: "重新实测本轮" })).not.toBeInTheDocument();
    expect(screen.queryByText(/新增一次.*分钟运行期限/)).not.toBeInTheDocument();
  });

  it("shows a Native retry error and releases the pending state without changing the job", async () => {
    const actions: Record<string, any>[] = [];
    installApi({ ...waitingJob(2, 2, false), status: "failed", pending_checkpoint: null, error: "map: schema: invalid result",
      ...{ can_retry_native: true, native_retry_context: {
        expected_task_id: "native-failed-1", budget_seconds: 1800, format_guidance_sha256: "a".repeat(64),
      } },
    }, (_path, body) => { actions.push(body); throw new Error("远端状态已经变化，请刷新"); });
    await openFailedJob();
    fireEvent.click(screen.getByRole("button", { name: "重新实测本轮" }));
    await waitFor(() => expect(toastErr).toHaveBeenCalledWith("重新实测失败", "远端状态已经变化，请刷新"));
    expect(screen.getByRole("button", { name: "重新实测本轮" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "本轮 Compile 失败" })).toHaveTextContent("map: schema: invalid result");
    expect(screen.queryByText(/人工重试 · 本次运行期限/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新实测本轮" }));
    await waitFor(() => expect(actions).toHaveLength(2));
    expect(actions[1].request_id).toBe(actions[0].request_id);
  });

  it("previews generated Skill files during mining without editing or submitting an incomplete candidate", async () => {
    installApi({ ...waitingJob(1, 2, false), status: "running", pending_checkpoint: null, rounds: [] });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    const preview = await screen.findByRole("region", { name: "Skill 文件预览（生成中）" });
    expect(within(preview).getByText("生成中 · 只读预览")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /提交.*进化|提交所选候选/ })).not.toBeInTheDocument();
    fireEvent.click(within(preview).getByRole("button", { name: "example / SKILL.md" }));
    const dialog = await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    expect(within(dialog).getByText("# Example")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(artifact.path)}`);
  });

  it("does not present an evaluation file alone as an early Skill preview", async () => {
    installApi({ ...waitingJob(1, 2, false), status: "running", pending_checkpoint: null, rounds: [],
      artifacts: [{ ...artifact, name: "EVALUATION.md", kind: "evaluation" }],
    });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    await screen.findByText("挖掘流水线（仅表示阶段位置）");
    expect(screen.queryByRole("region", { name: "Skill 文件预览（生成中）" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /提交.*进化|提交所选候选/ })).not.toBeInTheDocument();
  });

  it("hides frontmatter only in artifact previews and retains the full editable source", async () => {
    const raw = "---\nname: phone-appointment\ndescription: 中文说明\ncategory: workflow\n---\n\n# 电话约访\n\n中文步骤。";
    installApi(waitingJob());
    const baseApi = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => path.startsWith("/api/mining/artifacts/content?")
      ? { path: artifact.path, name: artifact.name, content: raw, size_bytes: raw.length }
      : baseApi(path, init));
    await openJob();
    fireEvent.click(screen.getByRole("button", { name: "example / SKILL.md" }));
    const preview = await screen.findByRole("region", { name: "Markdown 产物内容" });
    expect(preview).toHaveTextContent("# 电话约访");
    expect(preview).not.toHaveTextContent("name: phone-appointment");
    fireEvent.click(screen.getByRole("button", { name: "编辑产物" }));
    expect(screen.getByRole("textbox", { name: "Markdown 正文编辑器" })).toHaveValue(raw);
    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    expect(screen.getByRole("region", { name: "Markdown 编辑预览" })).not.toHaveTextContent("name: phone-appointment");
    fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
    expect(screen.getByRole("textbox", { name: "Markdown 正文编辑器" })).toHaveValue(raw);
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
  });

  it("creates an actual round cap and enables human Evidence checkpoints", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    const slider = await screen.findByRole("slider", { name: /挖掘轮数上限/ });
    fireEvent.change(slider, { target: { value: "2" } });
    expect(screen.getByRole("checkbox", { name: /有补证问题时等待人工补充 Evidence/ })).toBeChecked();
    expect(screen.getByText(/运行期限用于保护预算，不代表预计完成时间/)).toBeInTheDocument();
    expect(screen.queryByText(/内部证据复核次数提示/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(created.mock.calls[0]).toEqual(["/api/mining/jobs", expect.objectContaining({ max_rounds: 2, human_checkpoints: true, runtime_timeout_seconds: 3600 })]);
  });

  it("uses saved native defaults with source choices supplied entirely by the OV registry", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    const baseApi = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => path === "/api/mining/config" ? {
      max_rounds_default: 2, max_rounds_range: [1, 5], runtime_timeout_seconds_default: 600, runtime_timeout_seconds_range: [60, 7200],
    } : baseApi(path, init));
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    expect(await screen.findByRole("slider", { name: /挖掘轮数上限/ })).toHaveValue("2");
    expect(screen.getByRole("spinbutton", { name: /单轮运行期限/ })).toHaveValue(600);
    expect(screen.getByRole("combobox", { name: "选择 Skill 知识源" })).toHaveValue(source.path);
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith("/api/mining/jobs", expect.objectContaining({ max_rounds: 2, runtime_timeout_seconds: 600, input_dir: source.path })));
  });

  it("requires a complete goal for single Skill mode and sends it with a fixed count", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    fireEvent.change(screen.getByRole("combobox", { name: "挖掘模式" }), { target: { value: "single" } });
    expect(screen.getByRole("button", { name: "创建任务" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "挖掘方向" }), { target: { value: "完整客户对练沟通方法" } });
    expect(screen.getByRole("button", { name: "创建任务" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith("/api/mining/jobs", expect.objectContaining({
      mining_mode: "single", mining_direction: "完整客户对练沟通方法", target_skill_count: 1,
    })));
  });

  it("allows a target count without direction and rejects fractional counts", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    fireEvent.change(screen.getByRole("combobox", { name: "挖掘模式" }), { target: { value: "target" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "目标 Skill 数量" }), { target: { value: "2.5" } });
    expect(screen.getByRole("button", { name: "创建任务" })).toBeDisabled();
    fireEvent.change(screen.getByRole("spinbutton", { name: "目标 Skill 数量" }), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith("/api/mining/jobs", expect.objectContaining({
      mining_mode: "target", mining_direction: "", target_skill_count: 4,
    })));
  });

  it("loads source references cheaply and verifies only the explicitly selected directory", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    const baseApi = mockedApi.getMockImplementation()!;
    const unverified = { ...source, document_count: 0, total_bytes: 0, ready: false, status: "unverified" };
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/input-sources") return { sources: [unverified] };
      if (path === `/api/mining/input-sources/${source.source_id}`) return { source };
      return baseApi(path, init);
    });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    expect(await screen.findByRole("option", { name: /待检查/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "创建任务" })).toBeDisabled();
    expect(mockedApi).not.toHaveBeenCalledWith(`/api/mining/input-sources/${source.source_id}`);

    fireEvent.click(screen.getByRole("button", { name: "检查选中目录" }));
    await screen.findByText(`${source.path} · 2 个文档 · 200 B`);
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/input-sources/${source.source_id}`);
    expect(screen.getByRole("button", { name: "创建任务" })).toBeEnabled();
  });

  it("shows the actual extended round deadline instead of the configured base timeout", async () => {
    const initial = { ...waitingJob(), round_timeout_seconds: 7200, round_work_units: 12 };
    installApi(initial);
    await openJob();
    expect(screen.getByText("运行期限：2 小时 0 分")).toBeInTheDocument();
    expect(screen.queryByText("运行期限：1 小时 0 分")).not.toBeInTheDocument();
  });

  it.each([
    ["该来源超过 10000 个文件，请选择更小的目录。", "该来源超过 10000 个文件，请选择更小的目录。"],
    ["", "单个文本文件不超过 20 MB，目录不超过 10000 个文件；大来源分批处理"],
  ])("explains and blocks an over-limit source using the registry error or the capacity fallback (%s)", async (error, explanation) => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    const baseApi = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => path === "/api/mining/input-sources" ? {
      sources: [{ ...source, name: "大型来源", document_count: 10001, ready: false, status: "over_limit", ingestion: { error } }],
    } : baseApi(path, init));
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    expect(await screen.findByRole("option", { name: "大型来源（超过挖掘限额）" })).toBeDisabled();
    expect(screen.getByText(`超过挖掘限额：${error || "单个文本文件不超过 20 MB，目录不超过 10000 个文件；大来源分批处理。"}`)).toHaveTextContent(explanation);
    expect(screen.queryByText("该目录没有可挖掘文档，请先上传素材。")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建任务" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    expect(created).not.toHaveBeenCalled();
  });

  it("leaves an eligible source selectable when another source exceeds the mining limit", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    const baseApi = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => path === "/api/mining/input-sources" ? {
      sources: [
        { ...source, source_id: "too-large", path: "viking://resources/mining/too-large", name: "大型来源", document_count: 10001, ready: false, status: "over_limit" },
        source,
      ],
    } : baseApi(path, init));
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    expect(await screen.findByRole("option", { name: "大型来源（超过挖掘限额）" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "选择 Skill 知识源" })).toHaveValue(source.path);
    expect(screen.getByRole("button", { name: "创建任务" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith("/api/mining/jobs", expect.objectContaining({ source_id: source.source_id, input_dir: source.path })));
  });

  it("labels an over-limit source in the actual source workspace without treating its files as an empty directory", async () => {
    const { default: SkillSourceWorkspace } = await vi.importActual<typeof import("./SkillSourceWorkspace")>("./SkillSourceWorkspace");
    const error = "该来源超过 Skill 挖掘的 10000 个文件限额，请选择更小的来源目录";
    const overLimit = { ...source, name: "大型来源", document_count: 10001, ready: false, status: "over_limit", ingestion: { error } };
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return { accounts: ["default"], current: "default", root_uri: "viking://resources", sources: [overLimit] };
      if (path === "/api/mining/ov-source-tree?source_id=source-1") return {
        root_uri: source.path, exists: true, entries: [{ uri: `${source.path}/policy.md`, name: "policy.md", is_dir: false, size: 100 }],
      };
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[overLimit]} selectedPath={source.path} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    expect(screen.getByText("超过挖掘限额")).toHaveAttribute("title", error);
    expect(screen.queryByText("空目录")).not.toBeInTheDocument();
    expect(await screen.findByText("policy.md")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传文件" })).toBeEnabled();
  });

  it("prepares files, submits sourced Evidence and unknown questions, and reuses the request ID after a failed continue", async () => {
    const initial = waitingJob();
    const bodies: Record<string, any>[] = [];
    let attempt = 0;
    installApi(initial, (path, body) => {
      if (path.endsWith("/supplements")) {
        expect(body).toEqual({ checkpoint_id: "checkpoint-1", files: [{ name: "pricing.md", content_base64: "I+S7t+agvA==" }] });
        const saved = [{ name: "pricing.md", uri: "viking://resources/supplements/pricing.md" }];
        return { saved, job: { ...initial, pending_checkpoint: { ...initial.pending_checkpoint, attachments: saved } } };
      }
      if (path.endsWith("/continue")) {
        bodies.push(body);
        if (++attempt === 1) throw new Error("网络暂不可用");
        return { job: { ...initial, status: "running", current_round: 2, pending_checkpoint: null } };
      }
      throw new Error(`Unexpected action: ${path}`);
    });
    await openJob();
    expect(screen.getByText("运行中").parentElement).toHaveTextContent("0");
    expect(screen.queryByRole("button", { name: "删除任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提交到进化" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "问题 1 的回答" }), { target: { value: "除数为 9000，见最新计费规则。" } });
    fireEvent.change(screen.getByRole("textbox", { name: "问题 1 的 Evidence 来源" }), { target: { value: "viking://resources/pricing/rule.md\nviking://resources/pricing/rule.md" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 2 暂时无法确认" }));
    fireEvent.change(screen.getByRole("textbox", { name: "本轮复核意见" }), { target: { value: "请修正数值比较。" } });
    fireEvent.change(screen.getByLabelText("补充 Evidence 文件"), { target: { files: [new File(["#价格"], "pricing.md", { type: "text/markdown" })] } });
    await screen.findByText("pricing.md · viking://resources/supplements/pricing.md");
    fireEvent.click(screen.getByRole("button", { name: "提交补证并开始第 2 轮" }));
    await waitFor(() => expect(toastErr).toHaveBeenCalledWith("提交知识补证失败", "网络暂不可用"));
    fireEvent.click(screen.getByRole("button", { name: "提交补证并开始第 2 轮" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[0]).toEqual(expect.objectContaining({
      checkpoint_id: "checkpoint-1", answers: { q1: "除数为 9000，见最新计费规则。" },
      unresolved: ["q2"], references: { q1: ["viking://resources/pricing/rule.md"], q2: [] }, feedback: "请修正数值比较。",
      request_id: expect.any(String),
    }));
    expect(bodies[1].request_id).toBe(bodies[0].request_id);
    await screen.findByText("第 2 / 3 轮");
  });

  it("only offers finish at the final round and preserves unresolved Evidence", async () => {
    const initial = waitingJob(2, 2);
    const action = vi.fn((path, body) => ({ job: { ...initial, status: "succeeded", pending_checkpoint: null, finished_at: "2026-10-10T00:02:00Z" }, path, body }));
    installApi(initial, action);
    await openJob();
    expect(screen.queryByRole("button", { name: /开始第 3 轮/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 1 暂时无法确认" }));
    fireEvent.click(screen.getByRole("button", { name: "结束挖掘并保留最终产物" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith("/api/mining/jobs/job-1/finish", expect.objectContaining({ checkpoint_id: "checkpoint-2", unresolved: ["q1", "q2"], request_id: expect.any(String) })));
    await screen.findByRole("button", { name: "提交到进化" });
  });

  it("requires an answer or explicit unknown for a required question and omits a previous answer once unknown is selected", async () => {
    const initial = waitingJob();
    initial.pending_checkpoint.questions[0] = { ...initial.pending_checkpoint.questions[0], required: true } as typeof initial.pending_checkpoint.questions[0];
    const action = vi.fn(() => ({ job: { ...initial, status: "running", current_round: 2, pending_checkpoint: null } }));
    installApi(initial, action);
    await openJob();
    fireEvent.change(screen.getByRole("textbox", { name: "问题 1 的 Evidence 来源" }), { target: { value: "viking://resources/pricing/rule.md" } });
    fireEvent.click(screen.getByRole("button", { name: "提交补证并开始第 2 轮" }));
    expect(action).not.toHaveBeenCalled();
    expect(toastErr).toHaveBeenCalledWith("请确认必填 Evidence 问题", expect.stringContaining("来源引用不能代替回答"));
    fireEvent.change(screen.getByRole("textbox", { name: "问题 1 的回答" }), { target: { value: "这是尚未确认的推测" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 1 暂时无法确认" }));
    fireEvent.click(screen.getByRole("button", { name: "提交补证并开始第 2 轮" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith("/api/mining/jobs/job-1/continue", expect.objectContaining({ answers: {}, unresolved: ["q1", "q2"] })));
  });

  it("allows a checkpoint without questions to finish and makes archived artifacts read-only", async () => {
    const initial = waitingJob(2, 2, false);
    initial.rounds.unshift({ round: 1, artifacts: [{ ...artifact, path: "job-1/round-1/example/SKILL.md" }], ov_task_id: "compile-1", remote_warnings: [] });
    installApi(initial);
    await openJob();
    expect(screen.getByRole("button", { name: "结束挖掘并保留最终产物" })).toBeEnabled();
    const history = screen.getByRole("region", { name: "挖掘轮次记录" });
    fireEvent.click(within(history).getByText("第 1 轮 · 1 个产物 · 已归档"));
    fireEvent.click(within(history).getByRole("button", { name: "example / SKILL.md 只读" }));
    await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "example / SKILL.md" }));
    await screen.findByRole("button", { name: "编辑产物" });
  });

  it("shows partial-source warnings as plain text in a checkpoint, round history and final summary", async () => {
    const initial = waitingJob(1, 1);
    const warning = "Partial output: <img src=x onerror=alert(1)> source extraction failed.";
    initial.remote_warnings = [warning];
    initial.rounds[0].remote_warnings = [warning];
    installApi(initial, () => ({ job: { ...initial, status: "succeeded", pending_checkpoint: null } }));
    await openJob();
    const checkpointWarning = screen.getByRole("region", { name: "本轮 Compile 执行提示" });
    expect(checkpointWarning).toHaveTextContent(warning);
    expect(checkpointWarning).toHaveTextContent("部分来源未完整处理");
    expect(checkpointWarning.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByText("第 1 轮 · 1 个产物 · 当前轮次 · Compile 执行有提示"));
    const roundWarning = screen.getByRole("region", { name: "第 1 轮 Compile 执行提示" });
    expect(roundWarning).toHaveTextContent(warning);
    expect(roundWarning.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 1 暂时无法确认" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 2 暂时无法确认" }));
    fireEvent.click(screen.getByRole("button", { name: "结束挖掘并保留最终产物" }));
    const summary = await screen.findByRole("region", { name: "任务 Compile 执行提示" });
    expect(summary).toHaveTextContent(`第 1 轮：${warning}`);
    expect(summary.querySelectorAll("li")).toHaveLength(1);
    expect(summary.querySelector("img")).toBeNull();
  });

  it("classifies a failed Reduce truncation without claiming source extraction or round artifacts completed", async () => {
    const warning = "1 pipeline jobs failed: ['reduce: truncated: Output limit reached; reduce result size or use file references for agent files']";
    const initial = {
      ...waitingJob(), status: "failed", pending_checkpoint: null, artifacts: [], rounds: [],
      remote_warnings: [warning], finished_at: "2026-10-10T00:02:00Z",
      error: "COMPILE_INCOMPLETE: <img src=x onerror=alert(1)> No accepted final artifacts remain after failed jobs",
    };
    const action = vi.fn();
    installApi(initial, action);
    const failure = await openFailedJob();
    expect(failure).toHaveTextContent("Reduce 输出截断：模型输出达到单次长度上限。");
    expect(failure).not.toHaveTextContent("能力拆分规划");
    expect(failure).toHaveTextContent(initial.error);
    expect(failure.querySelector("img")).toBeNull();
    const warningRegion = screen.getByRole("region", { name: "任务 Compile 执行提示" });
    expect(warningRegion).toHaveTextContent(warning);
    expect(warningRegion).not.toHaveTextContent("来源曾未完整处理");
    expect(screen.getByText("第 1 轮 · 未生成可审核产物")).toBeInTheDocument();
    expect(screen.getByText("本轮未生成可审核产物。")).toBeInTheDocument();
    expect(screen.getByText(/^结束于 /)).toBeInTheDocument();
    expect(screen.queryByText(/^完成于 /)).not.toBeInTheDocument();
    expect(screen.queryByText("来源处理需复核")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /提交.*进化|提交补证|结束挖掘/ })).not.toBeInTheDocument();
    expect(action).not.toHaveBeenCalled();
  });

  it("uses structured pipeline errors when remote warnings are absent and preserves successful prior-round files as read-only", async () => {
    const initial = {
      ...waitingJob(2, 2, false), status: "failed", pending_checkpoint: null, artifacts: [],
      rounds: [{ round: 1, artifacts: [artifact], ov_task_id: "compile-1", remote_warnings: [] }],
      error: "COMPILE_INCOMPLETE: No accepted final artifacts remain after failed jobs",
      ov_pipeline: { errors: [{ stage: "reduce", code: "truncated", message: "Output limit reached" }] },
      finished_at: "2026-10-10T00:02:00Z",
    };
    installApi(initial);
    expect(await openFailedJob()).toHaveTextContent("Reduce 输出截断");
    expect(screen.getByRole("region", { name: "任务 Compile 执行提示" })).toHaveTextContent("reduce · truncated · Output limit reached");
    expect(screen.queryByText(/部分来源.*未完整处理/)).not.toBeInTheDocument();
    expect(screen.getByText("第 2 轮 · 未生成可审核产物")).toBeInTheDocument();
    const history = screen.getByRole("region", { name: "挖掘轮次记录" });
    fireEvent.click(within(history).getByText("第 1 轮 · 1 个产物 · 已归档"));
    fireEvent.click(within(history).getByRole("button", { name: "example / SKILL.md 只读" }));
    await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /提交.*进化/ })).not.toBeInTheDocument();
  });

  it.each([
    {
      remote_warnings: ["Unresolved output path compiled_skill/example/SKILL.md: reduce_merge: truncated: Output limit reached; reduce result size or use file references for agent files"],
      ov_pipeline: undefined,
    },
    {
      remote_warnings: [],
      ov_pipeline: { errors: [{ stage: "reduce_merge", code: "truncated", message: "Output limit reached" }] },
    },
  ])("identifies file collision merge truncation from its explicit stage without calling it capability planning ($remote_warnings)", async (diagnosis) => {
    const initial = {
      ...waitingJob(), status: "failed", pending_checkpoint: null, artifacts: [], rounds: [],
      ...diagnosis, error: "COMPILE_INCOMPLETE: No accepted final artifacts remain after failed jobs",
    };
    installApi(initial);
    const failure = await openFailedJob();
    expect(failure).toHaveTextContent("文件冲突合并（reduce_merge）输出截断");
    expect(failure).not.toHaveTextContent("能力拆分规划");
    expect(failure).not.toHaveTextContent("Reduce 输出截断");
    const warnings = screen.getByRole("region", { name: "任务 Compile 执行提示" });
    expect(warnings).toHaveTextContent("文件冲突合并（reduce_merge）的输出达到单次长度上限，被截断。");
    expect(warnings).not.toHaveTextContent("能力拆分规划");
    expect(warnings).not.toHaveTextContent("部分来源未完整处理");
  });

  it("does not infer a Reduce phase from a generic instruction to reduce result size", async () => {
    const initial = {
      ...waitingJob(), status: "failed", pending_checkpoint: null, artifacts: [], rounds: [],
      ov_stage: "compile: pipeline",
      remote_warnings: ["Writer warning: reduce result size because output limit reached"],
      error: "COMPILE_INCOMPLETE: transform rejected the response",
    };
    installApi(initial);
    expect(await openFailedJob()).toHaveTextContent("本轮 Compile 执行失败。");
    const warnings = screen.getByRole("region", { name: "任务 Compile 执行提示" });
    expect(warnings).toHaveTextContent("请结合任务状态和已有产物核对下方远端详情");
    expect(warnings).not.toHaveTextContent("Reduce 输出截断");
    expect(warnings).not.toHaveTextContent("文件冲突合并（reduce_merge）");
    expect(warnings).not.toHaveTextContent("能力拆分规划");
  });

  it("keeps unclassified Compile warnings generic rather than inventing a source or Reduce failure", async () => {
    const initial = {
      ...waitingJob(), status: "failed", pending_checkpoint: null, artifacts: [], rounds: [],
      remote_warnings: ["Partial output: source references could not be validated; writer failed."],
      error: "COMPILE_INCOMPLETE: transform rejected the response",
    };
    installApi(initial);
    expect(await openFailedJob()).toHaveTextContent("本轮 Compile 执行失败。");
    const warnings = screen.getByRole("region", { name: "任务 Compile 执行提示" });
    expect(warnings).toHaveTextContent("请结合任务状态和已有产物核对下方远端详情");
    expect(warnings).not.toHaveTextContent("来源曾未完整处理");
    expect(warnings).not.toHaveTextContent("能力拆分规划（Reduce）");
  });

  it("keeps the selected task and its Evidence clean when an earlier task upload finishes late", async () => {
    const initial = waitingJob();
    const second = {
      ...waitingJob(), job_id: "job-2", name: "第二个挖掘任务",
      pending_checkpoint: { ...initial.pending_checkpoint, id: "job-2-checkpoint", title: "第二个任务的 Evidence 复核" },
    };
    let resolveUpload!: (response: unknown) => void;
    let uploadStarted = false;
    installApi(initial, () => new Promise((resolve) => {
      resolveUpload = resolve;
      uploadStarted = true;
    }));
    const baseApi = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/jobs" && !init?.method) return {
        jobs: [initial, second], summary: { total: 2, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3 },
      };
      if (path === "/api/mining/jobs/job-2") return { job: second };
      return baseApi(path, init);
    });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /job-1/ }));
    await screen.findByText("本轮 Evidence 复核");
    fireEvent.change(screen.getByLabelText("补充 Evidence 文件"), { target: { files: [new File(["# Old task"], "old-task.md", { type: "text/markdown" })] } });
    await waitFor(() => expect(uploadStarted).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /job-2/ }));
    await screen.findByText("第二个任务的 Evidence 复核");
    const saved = [{ name: "old-task.md", uri: "viking://resources/job-1/supplements/old-task.md" }];
    await act(async () => resolveUpload({ saved, job: { ...initial, pending_checkpoint: { ...initial.pending_checkpoint, attachments: saved } } }));
    expect(screen.getByText("第二个任务的 Evidence 复核")).toBeVisible();
    expect(screen.queryByText("本轮 Evidence 复核")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "已准备补充 Evidence" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("补充 Evidence 文件")).toBeEnabled();
  });
});

function completedCandidateJob(names = ["procurement", "supplier", "contract"]) {
  const quality = {
    has_skill: true, level: "ready", label: "可提交", confidence: "候选级", summary: "Skill Candidate 结构完整。",
    warnings: [] as string[], artifacts: [], can_submit: true,
  };
  return {
    ...waitingJob(1, 1, false), status: "succeeded", pending_checkpoint: null,
    candidate_contract_version: 2, candidate_count: names.length,
    artifacts: names.map((name) => ({ ...artifact, skill_name: name, path: `job-1/workspace/compiled_skill/${name}/SKILL.md` })),
    artifact_quality: quality,
    candidate_quality: Object.fromEntries(names.map((name) => [name, { ...quality }])),
    capability_plan: {
      schema_version: "skill-miner.plan.v1",
      skills: names.map((name) => ({
        name, task: `${name} 的独立任务`, trigger: `${name} 请求`, inputs: ["申请材料"], outputs: ["处理结论"],
        boundary: "只处理有 Evidence 支持的流程", rationale: "任务可以独立执行和验证", source_refs: [`viking://resources/${name}/rule.md`],
      })),
      deferred: [{ reason: "付款流程的 Evidence 尚不完整", source_refs: ["viking://resources/payment.md"] }],
    },
  };
}

function candidateFiles(name: string, workspace = "job-1/workspace") {
  return [
    ["SKILL.md", "skill"], ["EVALUATION.md", "evaluation"],
    ["benchmark.json", "benchmark"], ["BENCHMARK.md", "benchmark"],
  ].map(([file, kind]) => ({ ...artifact, name: file, kind, skill_name: name, path: `${workspace}/compiled_skill/${name}/${file}` }));
}

function sharedFiles(workspace = "job-1/workspace") {
  return ["capability-plan.json", "evidence-map.md", "gaps.md", "review.json"].map((name) => ({
    ...artifact, name, kind: "semantic", skill_name: "", path: `${workspace}/semantic_reports/${name}`,
  }));
}

async function openCompletedJob(onNavigate?: (destination: "overview" | "sources" | "jobs" | "candidates" | "skills") => void) {
  render(<MiningView active page="jobs" onNavigate={onNavigate} />);
  fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
  await screen.findByRole("region", { name: "能力拆分计划" });
}

describe("MiningView independent Skill Candidates", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    vi.mocked(toastErr).mockClear();
    vi.mocked(toastOk).mockClear();
  });
  afterEach(cleanup);

  it("shows the generated Chinese title while the complete capability plan is still unavailable", async () => {
    const initial = {
      ...completedCandidateJob(["phone-appointment"]), status: "running", capability_plan: null,
      artifacts: [{ ...candidateFiles("phone-appointment")[0], display_title: "电话约访方法" }],
    };
    installApi(initial);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    const card = await screen.findByRole("region", { name: "Skill 文件预览（生成中）" });
    expect(within(card).getByText("电话约访方法")).toBeVisible();
    expect(within(card).getByText("标识：phone-appointment")).toBeVisible();
  });

  it("keeps each candidate's four files and capability details together, with shared analysis separate", async () => {
    const initial = completedCandidateJob(["returns", "shipping"]);
    initial.capability_plan.skills[0].task = "退货申请复核";
    initial.artifacts = [...candidateFiles("shipping"), ...sharedFiles(), ...candidateFiles("returns"), candidateFiles("returns")[0]];
    installApi(initial);
    await openCompletedJob();
    const shared = screen.getByRole("region", { name: "本轮共享资料" });
    for (const name of ["returns", "shipping"]) {
      const card = screen.getByRole("region", { name: `Skill Candidate ${name}` });
      const files = within(card).getAllByRole("button").filter((button) => button.getAttribute("aria-label")?.startsWith(`${name} / `));
      expect(files.map((file) => file.getAttribute("aria-label"))).toEqual([
        `${name} / SKILL.md`, `${name} / EVALUATION.md`, `${name} / benchmark.json`, `${name} / BENCHMARK.md`,
      ]);
      expect(card).toHaveTextContent(name === "returns" ? "退货申请复核" : `${name} 的独立任务`);
      expect(within(card).getByText(`标识：${name}`)).toBeVisible();
      expect(within(card).getByText("结构校验通过")).toBeVisible();
      expect(within(card).queryByRole("button", { name: /evidence-map/ })).not.toBeInTheDocument();
      expect(within(shared).queryByRole("button", { name: `${name} / SKILL.md` })).not.toBeInTheDocument();
    }
    expect(within(shared).getAllByRole("button")).toHaveLength(4);
    expect(within(shared).getByRole("region", { name: "能力拆分计划" })).not.toHaveTextContent("returns 的独立任务");
    fireEvent.click(within(screen.getByRole("region", { name: "Skill Candidate returns" })).getByRole("button", { name: "returns / EVALUATION.md" }));
    await screen.findByRole("dialog", { name: "产物预览 · EVALUATION.md" });
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(candidateFiles("returns")[1].path)}`);
    expect(screen.getByRole("button", { name: "编辑产物" })).toBeEnabled();
  });

  it.each(["rules.md", "configuration.json"])("groups %s with its Skill and keeps the resource preview read-only", async (file) => {
    const initial = completedCandidateJob(["returns", "shipping"]);
    const resource = {
      ...artifact, name: file, kind: "resource", skill_name: "returns",
      path: `job-1/workspace/compiled_skill/returns/references/nested/${file}`,
    };
    initial.artifacts = [...candidateFiles("returns"), ...candidateFiles("shipping"), resource];
    const action = vi.fn();
    installApi(initial, action);
    await openCompletedJob();
    const returns = screen.getByRole("region", { name: "Skill Candidate returns" });
    const resourceButton = within(returns).getByRole("button", { name: `returns / ${file} 只读` });
    expect(resourceButton).toHaveTextContent("配套资源");
    expect(within(screen.getByRole("region", { name: "Skill Candidate shipping" })).queryByText(file)).not.toBeInTheDocument();
    fireEvent.click(resourceButton);
    await screen.findByRole("dialog", { name: `产物预览 · ${file}` });
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(resource.path)}`);
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    expect(action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(within(returns).getByRole("button", { name: "returns / EVALUATION.md" }));
    await screen.findByRole("dialog", { name: "产物预览 · EVALUATION.md" });
    expect(screen.getByRole("button", { name: "编辑产物" })).toBeEnabled();
  });

  it("shows unplanned orphan files in a legacy job without expanding its submission whitelist", async () => {
    const initial = { ...completedCandidateJob(["legacy"]), candidate_contract_version: 1, capability_plan: null };
    initial.artifacts = [...candidateFiles("legacy"), ...candidateFiles("orphan").filter((file) => file.kind !== "skill"),
      { ...artifact, name: "notes.txt", kind: "other", skill_name: "orphan", path: "job-1/workspace/compiled_skill/orphan/notes.txt" }, ...sharedFiles()];
    const action = vi.fn((_path: string) => ({ created: true, job_id: "evolution-1", skill_name: "legacy" }));
    installApi(initial, action);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    const orphan = await screen.findByRole("region", { name: "Skill Candidate orphan" });
    expect(within(orphan).getByRole("button", { name: "orphan / EVALUATION.md" })).toBeEnabled();
    expect(within(orphan).getByRole("button", { name: "orphan / notes.txt" })).toHaveTextContent("文件");
    expect(within(orphan).getByRole("button", { name: "提交 orphan 到进化" })).toBeDisabled();
    expect(within(orphan).getByRole("checkbox", { name: "选择 orphan" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "提交到进化" }));
    await screen.findByText("legacy：已生成 Skill Candidate");
    expect(action).toHaveBeenCalledTimes(1);
    expect(action.mock.calls[0][0]).toBe("/api/mined-jobs/job-1/skills/legacy/submit");
  });

  it("retains shared files for a zero-candidate plan", async () => {
    const initial = completedCandidateJob([]);
    initial.artifacts = sharedFiles();
    installApi(initial);
    await openCompletedJob();
    expect(screen.getByRole("button", { name: "提交所选候选（0）" })).toBeDisabled();
    expect(within(screen.getByRole("region", { name: "本轮共享资料" })).getAllByRole("button")).toHaveLength(4);
    expect(screen.queryByRole("region", { name: "Skill Candidate 独立提交" })).not.toBeInTheDocument();
  });

  it("keeps long task warnings and deferred explanations collapsed while showing their counts", async () => {
    const initial = completedCandidateJob(["returns", "shipping"]);
    initial.artifacts = [...candidateFiles("returns"), ...candidateFiles("shipping"), ...sharedFiles()];
    initial.artifact_quality = { ...initial.artifact_quality, level: "caution", label: "需复核", warnings: Array.from({ length: 8 }, (_, index) => `本轮公共问题 ${index + 1}`) };
    initial.capability_plan.deferred = Array.from({ length: 5 }, (_, index) => ({ reason: `暂缓内容 ${index + 1}`, source_refs: ["viking://resources/deferred.md"] }));
    installApi(initial);
    await openCompletedJob();
    const quality = screen.getByRole("region", { name: "本轮复核提示" });
    expect(within(quality).getByText("需复核")).toBeVisible();
    expect(quality).toHaveTextContent("8 条提示 · 产物阶段：候选级");
    expect(quality).toHaveTextContent("结构校验通过仅确认产物格式");
    expect(screen.queryByText(/Skill 置信档/)).not.toBeInTheDocument();
    const warnings = within(quality).getByText("查看本轮复核提示（8 条）");
    expect(warnings.closest("details")).not.toHaveAttribute("open");
    expect(within(quality).getByText("• 本轮公共问题 1")).not.toBeVisible();
    const candidate = screen.getByRole("region", { name: "Skill Candidate returns" });
    expect(within(candidate).getByText("结构校验通过")).toBeVisible();
    expect(candidate).toHaveTextContent("本轮有 8 条公共提示");
    expect(within(candidate).queryByText("本轮公共问题 1")).not.toBeInTheDocument();
    fireEvent.click(warnings);
    expect(within(quality).getByText("• 本轮公共问题 1")).toBeVisible();
    const deferred = screen.getByText("暂缓形成 Skill Candidate 的内容（5 项）");
    expect(deferred.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("暂缓内容 1")).not.toBeVisible();
    fireEvent.click(deferred);
    expect(screen.getByText("暂缓内容 1")).toBeVisible();
  });

  it("groups archived candidates and shared analysis independently and keeps every preview read-only", async () => {
    const initial = waitingJob(2, 2, false);
    initial.rounds.unshift({ round: 1, artifacts: [...candidateFiles("returns", "job-1/round-1"), ...candidateFiles("shipping", "job-1/round-1"), ...sharedFiles("job-1/round-1")], ov_task_id: "compile-1", remote_warnings: [] });
    installApi(initial);
    await openJob();
    const history = screen.getByRole("region", { name: "挖掘轮次记录" });
    fireEvent.click(within(history).getByText("第 1 轮 · 12 个产物 · 已归档"));
    const returns = within(history).getByRole("region", { name: "第 1 轮 Skill Candidate returns" });
    expect(within(returns).getAllByRole("button")).toHaveLength(4);
    expect(within(returns).queryByRole("button", { name: /shipping/ })).not.toBeInTheDocument();
    const shared = within(history).getByRole("region", { name: "第 1 轮共享资料" });
    expect(within(shared).getAllByRole("button")).toHaveLength(4);
    fireEvent.click(within(returns).getByRole("button", { name: "returns / benchmark.json 只读" }));
    await screen.findByRole("dialog", { name: "产物预览 · benchmark.json" });
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent("job-1/round-1/compiled_skill/returns/benchmark.json")}`);
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(within(shared).getByRole("button", { name: "evidence-map.md 只读" }));
    await screen.findByRole("dialog", { name: "产物预览 · evidence-map.md" });
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
  });

  it("shows the capability boundaries and deferred Evidence, and submits one candidate idempotently", async () => {
    const initial = completedCandidateJob();
    initial.capability_plan.deferred[0].reason = "Evidence 不足：<img src=x onerror=alert(1)>";
    const action = vi.fn((_path, _body) => ({ created: action.mock.calls.length === 1, job_id: "evolution-1", skill_name: "supplier" }));
    const navigate = vi.fn();
    installApi(initial, action);
    await openCompletedJob(navigate);
    const plan = screen.getByRole("region", { name: "能力拆分计划" });
    expect(plan).toHaveTextContent("3 个 Skill Candidate");
    const supplier = screen.getByRole("region", { name: "Skill Candidate supplier" });
    expect(supplier).toHaveTextContent("supplier 的独立任务");
    fireEvent.click(within(supplier).getByText("任务边界与拆分依据"));
    expect(within(supplier).getByText("viking://resources/supplier/rule.md")).toBeVisible();
    expect(supplier).toHaveTextContent("只处理有 Evidence 支持的流程");
    expect(plan).not.toHaveTextContent("supplier 的独立任务");
    fireEvent.click(within(plan).getByText(/暂缓形成 Skill Candidate 的内容/));
    expect(within(plan).getByText(initial.capability_plan.deferred[0].reason)).toBeVisible();
    expect(plan.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提交 supplier 到进化" }));
    await screen.findByText("supplier：已生成 Skill Candidate");
    expect(action).toHaveBeenCalledTimes(1);
    expect(action).toHaveBeenLastCalledWith("/api/mined-jobs/job-1/skills/supplier/submit", { artifact_path: "job-1/workspace/compiled_skill/supplier/SKILL.md" });
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "提交 supplier 到进化" }));
    await screen.findByText("supplier：当前产物版本已在候选区中");
    expect(action.mock.calls[1]).toEqual(action.mock.calls[0]);
    expect(screen.getByRole("region", { name: "Skill Candidate 提交结果" })).toHaveTextContent("1 个成功，0 个失败");
    fireEvent.click(screen.getByRole("button", { name: /查看候选区/ }));
    expect(navigate).toHaveBeenCalledWith("candidates");
  });

  it("continues submitting after a candidate fails and retries only failures while preserving successful results", async () => {
    const initial = completedCandidateJob();
    let procurementAttempts = 0;
    const action = vi.fn((path: string) => {
      if (path.includes("/procurement/") && ++procurementAttempts === 1) throw new Error("暂时不可用");
      const parts = path.split("/");
      return { created: !path.includes("/procurement/"), job_id: "evolution-1", skill_name: parts[parts.length - 2] };
    });
    installApi(initial, action);
    await openCompletedJob();
    fireEvent.click(screen.getByRole("button", { name: "提交所选候选（3）" }));
    const report = await screen.findByRole("region", { name: "Skill Candidate 提交结果" });
    expect(report).toHaveTextContent("2 个成功，1 个失败");
    expect(report).toHaveTextContent("procurement：提交失败 · 暂时不可用");
    expect(action.mock.calls.map(([path]) => path)).toEqual([
      "/api/mined-jobs/job-1/skills/procurement/submit", "/api/mined-jobs/job-1/skills/supplier/submit", "/api/mined-jobs/job-1/skills/contract/submit",
    ]);
    expect(toastErr).toHaveBeenCalledWith("部分 Skill Candidate 提交成功", "2 个新建，0 个当前版本已在候选区，1 个失败");
    fireEvent.click(screen.getByRole("button", { name: "重试失败候选（1）" }));
    await waitFor(() => expect(report).toHaveTextContent("3 个成功，0 个失败"));
    expect(action).toHaveBeenCalledTimes(4);
    expect(action.mock.calls[3][0]).toBe("/api/mined-jobs/job-1/skills/procurement/submit");
    expect(report).toHaveTextContent("supplier：已生成 Skill Candidate");
    expect(report).toHaveTextContent("contract：已生成 Skill Candidate");
    expect(report).toHaveTextContent("procurement：当前产物版本已在候选区中");
    expect(screen.queryByRole("button", { name: /重试失败候选/ })).not.toBeInTheDocument();
  });

  it("allows eligible candidates to proceed independently and honors the selected subset", async () => {
    const initial = completedCandidateJob();
    initial.candidate_quality.contract = {
      ...initial.candidate_quality.contract, level: "incomplete", label: "不完整", summary: "contract 缺少评测定义", can_submit: false,
    };
    const action = vi.fn((_path: string) => ({ created: true, job_id: "evolution-1", skill_name: "supplier" }));
    installApi(initial, action);
    await openCompletedJob();
    expect(screen.getByRole("checkbox", { name: "选择 contract" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "选择 contract" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "提交 contract 到进化" })).toBeDisabled();
    expect(screen.getByText("contract 缺少评测定义")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 procurement" }));
    fireEvent.click(screen.getByRole("button", { name: "提交所选候选（1）" }));
    await screen.findByText("supplier：已生成 Skill Candidate");
    expect(action).toHaveBeenCalledTimes(1);
    expect(action.mock.calls[0][0]).toBe("/api/mined-jobs/job-1/skills/supplier/submit");
  });

  it("shows a valid zero-candidate plan and keeps submission unavailable", async () => {
    const initial = completedCandidateJob([]);
    const action = vi.fn();
    installApi(initial, action);
    await openCompletedJob();
    expect(screen.getByRole("region", { name: "能力拆分计划" })).toHaveTextContent("0 个 Skill Candidate");
    expect(screen.getByText("本轮没有形成 Skill Candidate，可查看共享分析了解原因。")).toBeVisible();
    fireEvent.click(screen.getByText(/暂缓形成 Skill Candidate 的内容/));
    expect(screen.getByText("付款流程的 Evidence 尚不完整")).toBeVisible();
    expect(screen.getByRole("button", { name: "提交所选候选（0）" })).toBeDisabled();
    expect(action).not.toHaveBeenCalled();
  });

  it("blocks further submissions while the selected batch is pending", async () => {
    let resolveFirst!: (response: unknown) => void;
    const action = vi.fn((path: string) => path.includes("/procurement/")
      ? new Promise((resolve) => { resolveFirst = resolve; })
      : { created: true, job_id: "evolution-1", skill_name: "supplier" });
    installApi(completedCandidateJob(["procurement", "supplier"]), action);
    await openCompletedJob();
    fireEvent.click(screen.getByRole("button", { name: "提交所选候选（2）" }));
    await waitFor(() => expect(action).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "提交 supplier 到进化" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "选择 supplier" })).toBeDisabled();
    await act(async () => resolveFirst({ created: true, job_id: "evolution-1", skill_name: "procurement" }));
    await screen.findByText("提交结果：2 个成功，0 个失败");
    expect(action).toHaveBeenCalledTimes(2);
  });

  it("refreshes candidate quality after an incomplete artifact is repaired", async () => {
    const initial = completedCandidateJob(["procurement", "supplier"]);
    initial.candidate_quality.supplier = {
      ...initial.candidate_quality.supplier, level: "incomplete", label: "不完整", summary: "supplier 的 Skill 内容无效", can_submit: false,
    };
    const revised = completedCandidateJob(["procurement", "supplier"]);
    const action = vi.fn((path: string, body: Record<string, any>) => {
      if (path === "/api/mining/artifacts/content") return {
        path: body.path, name: "SKILL.md", content: body.content, size_bytes: body.content.length, edited: true, job: revised,
      };
      throw new Error(`Unexpected action: ${path}`);
    });
    installApi(initial, action);
    await openCompletedJob();
    expect(screen.getByRole("button", { name: "提交 supplier 到进化" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /supplier \/ SKILL.md/ }));
    fireEvent.click(await screen.findByRole("button", { name: "编辑产物" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Markdown 正文编辑器" }), { target: { value: "# Repaired Skill" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith("/api/mining/artifacts/content", {
      path: initial.artifacts[1].path, content: "# Repaired Skill",
    }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Markdown 正文编辑器" })).toHaveValue("# Repaired Skill"));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "提交 supplier 到进化" })).toBeEnabled());
    expect(screen.queryByText("supplier 的 Skill 内容无效")).not.toBeInTheDocument();
  });

  it("keeps planned candidates without validated quality or SKILL files unavailable", async () => {
    const initial = completedCandidateJob();
    delete initial.candidate_quality.supplier;
    initial.candidate_quality.contract.has_skill = false;
    initial.artifacts = initial.artifacts.filter((item) => item.skill_name !== "contract");
    installApi(initial);
    await openCompletedJob();
    expect(screen.getByRole("button", { name: "提交所选候选（1）" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "提交 supplier 到进化" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "提交 contract 到进化" })).toBeDisabled();
  });

  it("retains confirmation for task-wide Evidence warnings without disabling an independently valid candidate", async () => {
    const initial = completedCandidateJob(["supplier"]);
    initial.artifact_quality = {
      ...initial.artifact_quality, level: "caution", label: "需复核", warnings: ["计费依据尚未确认", "计费依据尚未确认"],
    };
    const action = vi.fn(() => ({ created: true, job_id: "evolution-1", skill_name: "supplier" }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      installApi(initial, action);
      await openCompletedJob();
      expect(screen.getByRole("button", { name: "提交 supplier 到进化" })).toBeEnabled();
      fireEvent.click(screen.getByRole("button", { name: "提交 supplier 到进化" }));
      expect(action).not.toHaveBeenCalled();
      expect(confirm).toHaveBeenCalledWith(expect.stringContaining("计费依据尚未确认"));
      expect(String(confirm.mock.calls[0][0]).match(/计费依据尚未确认/g)).toHaveLength(1);
      confirm.mockReturnValue(true);
      fireEvent.click(screen.getByRole("button", { name: "提交 supplier 到进化" }));
      await screen.findByText("supplier：已生成 Skill Candidate");
      expect(action).toHaveBeenCalledTimes(1);
    } finally {
      confirm.mockRestore();
    }
  });

  it("shows an automatic empty-review receipt without rendering a human finish form", async () => {
    const initial = {
      ...completedCandidateJob(["parent"]), status: "succeeded", pending_checkpoint: null,
      skipped_checkpoints: [{ round: 2, reason: "本轮无补证问题，自动跳过人工补证" }],
    };
    installApi(initial);
    await openCompletedJob();
    expect(screen.getByText("第 2 轮没有补证问题，已自动跳过人工补证。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "结束挖掘并保留最终产物" })).not.toBeInTheDocument();
  });

  it("preserves submission and navigation for historical single-candidate jobs", async () => {
    const initial = { ...waitingJob(1, 1, false), status: "succeeded", pending_checkpoint: null };
    const action = vi.fn(() => ({ created: true, job_id: "evolution-1", skill_name: "example" }));
    const navigate = vi.fn();
    installApi(initial, action);
    render(<MiningView active page="jobs" onNavigate={navigate} />);
    fireEvent.click(await screen.findByRole("button", { name: /真实多轮挖掘/ }));
    fireEvent.click(await screen.findByRole("button", { name: "提交到进化" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("candidates"));
    expect(action).toHaveBeenCalledTimes(1);
    expect(action.mock.calls[0]).toEqual([
      "/api/mined-jobs/job-1/skills/example/submit", { artifact_path: artifact.path },
    ]);
  });
});

describe("OpenViking mining settings", () => {
  beforeEach(() => mockedApi.mockReset());
  afterEach(cleanup);

  const settings = {
    engine: "ov_compile", model: { configuration_source: "openviking_server", editable_here: false },
    pipeline: { max_rounds: 3, runtime_timeout_seconds: 3600 },
    prompts: [{ id: "ov_compile", label: "Skill 挖掘编译", description: "生成可复核的候选与 Test Dataset。",
      default_prompt: "Default Compile", effective_prompt: "Default Compile", overridden: false, char_count: 15 }],
  };

  it("edits only actual OV task defaults and the Compile instruction, then restores its default", async () => {
    const { default: Whitebox } = await vi.importActual<typeof import("./MiningWhiteboxPanel")>("./MiningWhiteboxPanel");
    const requests: Array<{ path: string; body?: Record<string, any> }> = [];
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = init.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ path, body });
        if (path.endsWith("/reset")) return settings;
        return { ...settings, pipeline: body.pipeline, prompts: [{ ...settings.prompts[0], effective_prompt: body.prompts[0].prompt, overridden: true }] };
      }
      return settings;
    });
    render(<Whitebox active user={{ role: "admin" } as UserProfile} />);
    const rounds = await screen.findByRole("spinbutton", { name: "默认挖掘轮数上限" });
    expect(rounds).toHaveAttribute("max", "5");
    fireEvent.change(rounds, { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "默认单轮运行期限（秒）" }), { target: { value: "600" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Skill 挖掘编译指令" }), { target: { value: "Review source Evidence." } });
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(screen.queryByText("模型瞬时错误重试次数")).not.toBeInTheDocument();
    expect(screen.queryByText("Benchmark 目标题量")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存参数与编译指令" }));
    await waitFor(() => expect(requests[0]).toEqual({
      path: "/api/mining/settings", body: {
        pipeline: { max_rounds: 2, runtime_timeout_seconds: 600 },
        prompts: [{ id: "ov_compile", prompt: "Review source Evidence." }],
      },
    }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复默认" }));
    await waitFor(() => expect(requests[1]).toEqual({ path: "/api/mining/prompts/ov_compile/reset", body: undefined }));
    expect(screen.getByRole("textbox", { name: "Skill 挖掘编译指令" })).toHaveValue("Default Compile");
  });

  it("keeps the Compile instruction and task defaults read-only for a non-admin", async () => {
    const { default: Whitebox } = await vi.importActual<typeof import("./MiningWhiteboxPanel")>("./MiningWhiteboxPanel");
    mockedApi.mockResolvedValue(settings);
    render(<Whitebox active />);
    expect(await screen.findByRole("textbox", { name: "Skill 挖掘编译指令" })).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: "默认挖掘轮数上限" })).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: "默认单轮运行期限（秒）" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存参数与编译指令" })).toBeDisabled();
  });
});
