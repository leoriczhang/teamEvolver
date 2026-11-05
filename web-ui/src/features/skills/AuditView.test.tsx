// @vitest-environment jsdom

import { api } from "@/api/client";
import AuditView from "@/features/skills/AuditView";
import "@testing-library/jest-dom/vitest";
import { act,cleanup,render,screen,within } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const completedCycle = {
  command_id: "evolve:audit-zero-candidates",
  created_at: "2026-10-22T09:01:51Z",
  finished_at: "2026-10-22T09:01:52Z",
  result: {
    status: "completed",
    sessions: 9,
    evolutions: [
      {
        skill_name: "knowledge-15001", action: "skip",
        rationale: "insufficient distinct users for team evidence", session_ids: ["session-one", "session-two"],
      },
      {
        skill_name: "knowledge-15002", action: "skip",
        rationale: "insufficient distinct Sessions for team evidence", session_ids: ["session-three"],
      },
    ],
    candidates_queued: 0,
  },
};

function expectStat(label: string, value: number) {
  const card = screen.getByText(label).parentElement;
  expect(card).not.toBeNull();
  expect(within(card!).getByText(String(value), { exact: true })).toBeVisible();
}

describe("AuditView", () => {
  beforeEach(() => { mockedApi.mockReset(); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("shows completed zero-candidate results and the evidence gate that skipped each Skill group", async () => {
    mockedApi.mockResolvedValue({ reachable: true, cycles: [completedCycle] });
    render(<AuditView active />);

    expect(await screen.findByText("knowledge-15001")).toBeVisible();
    expect(screen.getByText("knowledge-15002")).toBeVisible();
    expect(screen.getByText(completedCycle.command_id, { exact: false })).toBeVisible();
    expect(document.body).toHaveTextContent(new Date(completedCycle.created_at).toLocaleString("zh-CN", { hour12: false }));
    expect(document.body).toHaveTextContent(new Date(completedCycle.finished_at).toLocaleString("zh-CN", { hour12: false }));
    expectStat("周期数", 1);
    expectStat("涉及会话", 9);
    expectStat("排队候选", 0);
    expect(screen.getByText(/9 会话 \/ 2 技能组/)).toBeVisible();
    expect(screen.getAllByText("跳过", { exact: true })).toHaveLength(2);
    expect(screen.getByText(/不同用户.*不足/)).toBeVisible();
    expect(screen.getByText(/不同.*Session.*不足|不同会话.*不足/)).toBeVisible();
    expect(screen.queryByText(/planner 判断当前 Skill 无需优化/)).not.toBeInTheDocument();
  });

  it("shows the created candidate and its identifier from the nested execution result", async () => {
    mockedApi.mockResolvedValue({
      reachable: true,
      cycles: [{
        ...completedCycle,
        command_id: "evolve:audit-candidate-created",
        result: {
          status: "completed", sessions: 4, candidates_queued: 1,
          evolutions: [{
            skill_name: "knowledge-15001", action: "candidate_created", candidate_id: "candidate-15001-r2",
            candidate_revision: 2, rationale: "Clarify the brand positioning example.",
            session_ids: ["session-one", "session-two", "session-three", "session-four"],
          }],
        },
      }],
    });
    render(<AuditView active />);

    expect(await screen.findByText("knowledge-15001")).toBeVisible();
    expectStat("涉及会话", 4);
    expectStat("排队候选", 1);
    expect(screen.getByText(/4 会话 \/ 1 技能组/)).toBeVisible();
    expect(screen.getByText("candidate-15001-r2", { exact: false })).toBeVisible();
    expect(screen.getByText("Clarify the brand positioning example.")).toBeVisible();
    expect(screen.queryByText("无变更", { exact: true })).not.toBeInTheDocument();
  });

  it("keeps historical flat audit records readable", async () => {
    mockedApi.mockResolvedValue({
      cycles: [{
        timestamp: "2026-10-20T01:02:03Z", sessions: 3, skill_groups: 1, actions: 1,
        uploaded_skills: 1, candidates_queued: 0, session_ids: ["legacy-session"],
        evolutions: [{ skill_name: "legacy-skill", action: "update", uploaded: true, reason: "Legacy audit evidence." }],
      }],
    });
    render(<AuditView active />);

    expect(await screen.findByText("legacy-skill")).toBeVisible();
    expectStat("周期数", 1);
    expectStat("涉及会话", 3);
    expectStat("上传技能", 1);
    expectStat("排队候选", 0);
    expect(screen.getByText("legacy-session")).toBeVisible();
    expect(screen.getByText("Legacy audit evidence.")).toBeVisible();
  });

  it("does not count bootstrap or model-test command records as evolution cycles", async () => {
    mockedApi.mockResolvedValue({
      reachable: true,
      cycles: [
        { ...completedCycle, command_id: "ov-bootstrap:directory-check", result: { status: "completed", sessions: 500 } },
        { ...completedCycle, command_id: "model-test:connection-check", result: { status: "completed", candidates_queued: 500 } },
        completedCycle,
      ],
    });
    render(<AuditView active />);

    expect(await screen.findByText("knowledge-15001")).toBeVisible();
    expectStat("周期数", 1);
    expectStat("涉及会话", 9);
    expectStat("排队候选", 0);
    expect(screen.queryByText(/ov-bootstrap:directory-check/)).not.toBeInTheDocument();
    expect(screen.queryByText(/model-test:connection-check/)).not.toBeInTheDocument();
  });

  it("refreshes a running task until the persisted result arrives and then stops polling", async () => {
    vi.useFakeTimers();
    let historyReads = 0;
    mockedApi.mockImplementation(path => {
      if (path.startsWith("/history?")) {
        historyReads++;
        return Promise.resolve({
          reachable: true,
          cycles: [historyReads === 1 ? { ...completedCycle, result: null, finished_at: null } : completedCycle],
        });
      }
      return Promise.resolve({ status: "running" });
    });
    render(<AuditView active />);
    await act(async () => {});

    expect(screen.getByText("运行中", { exact: true })).toBeVisible();
    expect(screen.getByText(/结果尚未生成/)).toBeVisible();
    expect(screen.queryByText(/已完成 ·/)).not.toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledWith("/api/jobs/evolve%3Aaudit-zero-candidates");
    expect(historyReads).toBe(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText("已完成 · 全部分组跳过")).toBeVisible();
    expect(screen.getByText("knowledge-15001")).toBeVisible();
    expectStat("涉及会话", 9);
    expectStat("排队候选", 0);
    expect(historyReads).toBe(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(historyReads).toBe(2);
    expect(mockedApi.mock.calls.filter(([path]) => path.startsWith("/api/jobs/"))).toHaveLength(1);
  });

  it("waits for a slow poll without overlapping requests or discarding the completed result", async () => {
    vi.useFakeTimers();
    let historyReads = 0;
    let finishPoll!: (value: { cycles: typeof completedCycle[] }) => void;
    mockedApi.mockImplementation(path => {
      if (!path.startsWith("/history?")) return Promise.resolve({ status: "running" });
      historyReads++;
      if (historyReads === 1) {
        return Promise.resolve({ cycles: [{ ...completedCycle, result: null, finished_at: null }] });
      }
      return new Promise(resolve => { finishPoll = resolve; });
    });
    render(<AuditView active />);
    await act(async () => {});
    expect(screen.getByText("运行中", { exact: true })).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(historyReads).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(historyReads).toBe(2);

    await act(async () => { finishPoll({ cycles: [completedCycle] }); });
    expect(screen.getByText("已完成 · 全部分组跳过")).toBeVisible();
    expectStat("涉及会话", 9);
    expect(screen.getByText(/不同用户.*不足/)).toBeVisible();
    expect(screen.getByText(/不同.*Session.*不足|不同会话.*不足/)).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(historyReads).toBe(2);
  });

  it.each<[string, string, boolean]>([
    ["queued", "排队中", true],
    ["retry_wait", "等待重试", true],
    ["failed", "失败", false],
    ["cancelled", "已取消", false],
  ])("shows %s task status and errors without claiming the task completed", async (status, label, shouldPoll) => {
    vi.useFakeTimers();
    mockedApi.mockImplementation(path => path.startsWith("/history?")
      ? Promise.resolve({ cycles: [{ ...completedCycle, result: null, finished_at: null }] })
      : Promise.resolve({ status, error_code: "UPSTREAM_UNAVAILABLE" }));
    render(<AuditView active />);
    await act(async () => {});

    expect(screen.getByText(label, { exact: true })).toBeVisible();
    expect(screen.getByText(/任务错误：UPSTREAM_UNAVAILABLE/)).toBeVisible();
    expect(screen.queryByText(/已完成 ·/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 会话 \/ 0 技能组/)).not.toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(mockedApi.mock.calls.filter(([path]) => path.startsWith("/history?")))
      .toHaveLength(shouldPoll ? 2 : 1);
  });

  it("shows an unknown state when the job status cannot be loaded", async () => {
    mockedApi.mockImplementation(path => path.startsWith("/history?")
      ? Promise.resolve({ cycles: [{ ...completedCycle, result: null, finished_at: null }] })
      : Promise.reject(new Error("status endpoint unavailable")));
    render(<AuditView active />);

    expect(await screen.findByText("状态待确认", { exact: true })).toBeVisible();
    expect(screen.getByText(completedCycle.command_id, { exact: false })).toBeVisible();
    expect(screen.queryByText("运行中", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText(/已完成 ·/)).not.toBeInTheDocument();
  });

  it("does not load while inactive and refreshes when the audit tab is activated again", async () => {
    mockedApi.mockResolvedValue({ cycles: [completedCycle] });
    const view = render(<AuditView active={false} />);
    expect(mockedApi).not.toHaveBeenCalled();

    view.rerender(<AuditView active />);
    expect(await screen.findByText("knowledge-15001")).toBeVisible();
    expect(mockedApi).toHaveBeenCalledTimes(1);

    view.rerender(<AuditView active={false} />);
    expect(mockedApi).toHaveBeenCalledTimes(1);
    mockedApi.mockResolvedValue({ cycles: [{ ...completedCycle, result: { ...completedCycle.result, sessions: 12 } }] });
    view.rerender(<AuditView active />);
    expect(await screen.findByText(/12 会话 \/ 2 技能组/)).toBeVisible();
    expectStat("涉及会话", 12);
    expect(mockedApi).toHaveBeenCalledTimes(2);
  });
});
