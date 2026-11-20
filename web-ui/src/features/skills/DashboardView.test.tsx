// @vitest-environment jsdom
import { api } from "@/api/client";
import DashboardView from "@/features/skills/DashboardView";
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
const mockApi = vi.mocked(api);
let failSkills = false;
beforeEach(() => {
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
