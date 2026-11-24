// @vitest-environment jsdom
import { api } from "@/api/client";
import DashboardView from "@/features/skills/DashboardView";
import { toastErr,toastOk } from "@/lib/toast";
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";

vi.mock("@/api/client", async importOriginal => ({
  ...await importOriginal<typeof import("@/api/client")>(), api: vi.fn(),
}));
vi.mock("@/features/sessions/hooks/useSessionList", () => ({
  useSessionList: () => ({
    rows: [], total: 0, page: 1, setPage: vi.fn(), reload: vi.fn(), stats: null, skillCounts: [],
  }),
}));
vi.mock("@/features/skills/dashboard/SetupChecklist", () => ({ default: () => null }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
const command = vi.hoisted(() => vi.fn());
vi.mock("@/features/skills/api/skillJobs", () => ({ runSkillCommand: command }));
const mockApi = vi.mocked(api);
let failSkills = false;
beforeEach(() => {
  vi.clearAllMocks();
  command.mockReset();
  failSkills = false;
  window.history.replaceState(null, "", "/");
  mockApi.mockReset();
  mockApi.mockImplementation(async path => {
    if (path === "/api/skills") {
      if (failSkills) throw new Error("MA HTTP 503: CloudUnavailable");
      return { skills: [{ name: "video-core", skill_id: "remote-core", version: 53, connection_id: "video" }] };
    }
    if (path.startsWith("/api/skill-candidates")) return { candidates: [], total: 0 };
    if (path === "/api/ma/connections") return { connections: [] };
    return {};
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("distinguishes cloud publication from actual Agent bindings", async () => {
  const original = mockApi.getMockImplementation()!;
  mockApi.mockImplementation(async (path, options) => path === "/api/skills" ? { skills: [
    { name: "unmounted", version: 2, connection_id: "video", bound_versions: {} },
    { name: "mounted", version: 2, connection_id: "video", bound_versions: { root: "2" } },
    { name: "outdated", version: 2, connection_id: "video", bound_versions: { root: "1" } },
  ] } : original(path, options));
  render(<DashboardView active onNavigate={vi.fn()} />);
  expect(await screen.findByText("已发布，未挂载")).toBeVisible();
  expect(screen.getByText("已挂载")).toBeVisible();
  expect(screen.getByText("已挂载旧版本")).toBeVisible();
});

function publishCandidateFixture() {
  let published = false;
  const evaluation = {
    status: "evaluated", recommended_publish: false,
    replay: { validation_method: "experience_review", verdict: "inconclusive",
      runtime_gate: { status: "not_required" }, assessment: {
        source: "model", verdict: "inconclusive", scope: "工具指引", reason: "证据不足", evidence_refs: [],
      } },
  };
  const candidate = { job_id: "force-fixture", skill_name: "video-core", candidate_revision: 3,
    current_skill_md: "BASELINE", candidate_skill_md: "CANDIDATE", evaluation };
  const original = mockApi.getMockImplementation()!;
  mockApi.mockImplementation(async (path, options) => {
    if (path === "/api/skills") return { skills: [{ name: "video-core", version: published ? 54 : 53 }] };
    if (path.startsWith("/api/skill-candidates/force-fixture/")) return candidate;
    if (path.startsWith("/api/skill-candidates?")) return {
      candidates: published ? [] : [candidate], total: published ? 0 : 1,
    };
    return original(path, options);
  });
  command.mockResolvedValue(evaluation);
  return { markPublished: () => { published = true; } };
}

it("sends explicit full AB evaluation from the dashboard without publishing", async () => {
  publishCandidateFixture();
  render(<DashboardView active onNavigate={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看回放并发布" }));
  fireEvent.click(await screen.findByRole("button", { name: "完整 A/B" }));
  await waitFor(() => expect(command).toHaveBeenCalledWith(
    "/api/skill-candidates/force-fixture/evaluate?refresh=true",
    expect.objectContaining({ body: JSON.stringify({ validation_method: "full_ab" }) }),
  ));
  expect(command.mock.calls.some(([path]) => path.endsWith("/validate"))).toBe(false);
});

it("refreshes the Skill version after forced publication finishes", async () => {
  const fixture = publishCandidateFixture();
  let finish!: (value: unknown) => void;
  command.mockImplementation(path => path.endsWith("/validate")
    ? new Promise(done => { finish = done; }) : Promise.resolve({}));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<DashboardView active onNavigate={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看回放并发布" }));
  fireEvent.click(await screen.findByRole("button", { name: "强制发布" }));
  expect(await screen.findByRole("button", { name: "发布中…" })).toBeDisabled();
  expect(command).toHaveBeenCalledWith("/api/skill-candidates/force-fixture/validate", expect.objectContaining({
    body: JSON.stringify({ mode: "force", candidate_revision: 3 }),
  }));
  fixture.markPublished();
  finish({ status: "published", accepted: true, version: 54 });
  expect(await screen.findByText("v54")).toBeVisible();
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "查看回放并发布" })).not.toBeInTheDocument();
  expect(toastOk).toHaveBeenCalledWith("发布结果", "published (v54)");
});

it("keeps an unpublished candidate open and reports the blocking result", async () => {
  publishCandidateFixture();
  command.mockResolvedValue({ status: "inconclusive", evaluation: { replay: { reason: "执行验证门禁未通过" } } });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<DashboardView active onNavigate={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看回放并发布" }));
  fireEvent.click(await screen.findByRole("button", { name: "强制发布" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("执行验证门禁未通过");
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(screen.getByText("v53")).toBeVisible();
  expect(toastErr).toHaveBeenCalledWith("发布失败", "执行验证门禁未通过");
  expect(toastOk).not.toHaveBeenCalledWith("发布结果", expect.anything());
});

it("shows failed Skill loading and recovers on retry instead of calling it an empty registry", async () => {
  failSkills = true;
  render(<DashboardView active onNavigate={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Skills 加载失败");
  expect(screen.queryByText("注册表为空")).not.toBeInTheDocument();
  failSkills = false;
  fireEvent.click(screen.getByRole("button", { name: "重试加载 Skills" }));
  expect(await screen.findByText("video-core")).toBeVisible();
  expect(screen.getByText("v53")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps last successful cloud Skills visible if a refresh fails", async () => {
  render(<DashboardView active onNavigate={vi.fn()} />);
  await screen.findByText("video-core");
  failSkills = true;
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("上次成功加载的数据"));
  expect(screen.getByText("video-core")).toBeVisible();
  expect(screen.getByText("v53")).toBeVisible();
});
