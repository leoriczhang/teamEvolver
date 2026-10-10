// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import { toastErr } from "@/lib/toast";
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

type FixtureJob = ReturnType<typeof waitingJob>;

function installApi(job: FixtureJob | null, onAction?: (path: string, body: Record<string, any>) => unknown) {
  let current = job;
  mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === "/api/mining/config") return {
      input_dirs: [source.path], input_sources: [source], default_input_dir: source.path,
      max_rounds_default: 3, max_rounds_range: [1, 5], compiled_skills: [], compiled_skill_details: [], checkpoints: [],
      benchmark: { default_dist: "", default_total: 12 },
    };
    if (path === "/api/mining/ov-sources") return { sources: [source] };
    if (path === "/api/mining/jobs" && !init?.method) return {
      jobs: current ? [current] : [], summary: { total: current ? 1 : 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3 },
    };
    if (path === "/api/mining/jobs/job-1" && !init?.method) return { job: current };
    if (path.startsWith("/api/mining/artifacts/content?")) {
      return { path: decodeURIComponent(path.split("path=")[1]), name: "SKILL.md", content: "# Example", size_bytes: 9 };
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

describe("MiningView real mining rounds", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    vi.mocked(toastErr).mockClear();
  });
  afterEach(cleanup);

  it("creates an actual round cap and enables human Evidence checkpoints", async () => {
    const created = vi.fn(() => ({ jobs: [] }));
    installApi(null, created);
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: "新建 Skill 挖掘" }));
    const slider = await screen.findByRole("slider", { name: /挖掘轮数上限/ });
    fireEvent.change(slider, { target: { value: "2" } });
    expect(screen.getByRole("checkbox", { name: /每轮完成后等待人工补充 Evidence/ })).toBeChecked();
    expect(screen.queryByText(/内部证据复核次数提示/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(created.mock.calls[0]).toEqual(["/api/mining/jobs", expect.objectContaining({ max_rounds: 2, human_checkpoints: true, runtime_timeout_seconds: 3600 })]);
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
    fireEvent.click(within(history).getByRole("button", { name: "SKILL.md 只读" }));
    await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    expect(screen.queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: /example \/ SKILL.md/ }));
    await screen.findByRole("button", { name: "编辑产物" });
  });

  it("shows partial-source warnings as plain text in a checkpoint, round history and final summary", async () => {
    const initial = waitingJob(1, 1);
    const warning = "Partial output: <img src=x onerror=alert(1)> source extraction failed.";
    initial.remote_warnings = [warning];
    initial.rounds[0].remote_warnings = [warning];
    installApi(initial, () => ({ job: { ...initial, status: "succeeded", pending_checkpoint: null } }));
    await openJob();
    const checkpointWarning = screen.getByRole("region", { name: "本轮 Compile 来源处理警告" });
    expect(checkpointWarning).toHaveTextContent(warning);
    expect(checkpointWarning).toHaveTextContent("部分来源未完整处理");
    expect(checkpointWarning.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByText("第 1 轮 · 1 个产物 · 当前轮次 · 来源处理有警告"));
    const roundWarning = screen.getByRole("region", { name: "第 1 轮 Compile 来源处理警告" });
    expect(roundWarning).toHaveTextContent(warning);
    expect(roundWarning.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 1 暂时无法确认" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "问题 2 暂时无法确认" }));
    fireEvent.click(screen.getByRole("button", { name: "结束挖掘并保留最终产物" }));
    const summary = await screen.findByRole("region", { name: "任务 Compile 来源处理警告" });
    expect(summary).toHaveTextContent(`第 1 轮：${warning}`);
    expect(summary.querySelectorAll("li")).toHaveLength(1);
    expect(summary.querySelector("img")).toBeNull();
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
