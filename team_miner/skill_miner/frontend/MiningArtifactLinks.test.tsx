// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor,within } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

import { api } from "@/api/client";
import { toastErr } from "@/lib/toast";
import MiningView from "./MiningView";
import type { MiningArtifact,MiningJob } from "./types";

vi.mock("@/api/client", () => ({ api: vi.fn(), tenantHeaders: (_path: string, init?: HeadersInit) => new Headers(init) }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
vi.mock("@skill-miner/MiningWhiteboxPanel", () => ({ default: () => null }));
vi.mock("@skill-miner/SkillSourceWorkspace", () => ({ default: () => null }));

const mockedApi = vi.mocked(api);
const workspace = "mining_jobs/job-1/workspace/compiled_skill/refund";
function artifact(path: string, kind: MiningArtifact["kind"]): MiningArtifact {
  return { name: path.split("/").pop()!, path, kind, size_bytes: 100, skill_name: "refund" };
}
const skill = artifact(`${workspace}/SKILL.md`, "skill");
const config = artifact(`${workspace}/references/knowledge-config.json`, "resource");
const guide = artifact(`${workspace}/references/说明.md`, "resource");
const job: MiningJob = {
  job_id: "job-1", name: "资源链接验收", status: "succeeded", input_dir: "viking://resources/source",
  document_count: 1, current_round: 2, max_rounds: 2,
  phase: { step1: "done", step2: "done", step3: "done" },
  created_at: "2026-10-22T00:00:00Z", started_at: "2026-10-22T00:00:00Z", finished_at: "2026-10-22T00:01:00Z",
  updated_at: "2026-10-22T00:01:00Z", error: "", stop_reason: "", artifacts: [skill, config, guide],
};

function installApi(current: MiningJob = job) {
  mockedApi.mockImplementation(async (path: string) => {
    if (path === "/api/mining/config") return { max_rounds_default: 2, max_rounds_range: [1, 5] };
    if (path === "/api/mining/input-sources") return { sources: [] };
    if (path === "/api/mining/jobs") return { jobs: [current] };
    if (path === "/api/mining/jobs/job-1") return { ok: true, job: current };
    if (path.startsWith("/api/mining/artifacts/content?")) {
      const target = new URLSearchParams(path.split("?")[1]).get("path")!;
      const content = target.endsWith("SKILL.md")
        ? "# 退款教官\n\n[知识点配置](references/knowledge-config.json)\n\n[说明](references/%E8%AF%B4%E6%98%8E.md)\n\n[不存在](references/missing.json)\n\n[越界](../../../../other-job/SKILL.md)"
        : target.endsWith("说明.md") ? "# 说明\n\n[返回 Skill](../SKILL.md)" : "{\"knowledge_id\":\"refund\"}";
      return { path: target, name: target.split("/").pop(), content, size_bytes: content.length };
    }
    throw new Error(`Unexpected API: ${path}`);
  });
}

async function openSkill() {
  render(<MiningView active page="jobs" />);
  fireEvent.click(await screen.findByRole("button", { name: /资源链接验收/ }));
  fireEvent.click(await screen.findByRole("button", { name: "refund / SKILL.md" }));
  return screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
}

describe("Mining artifact Markdown links", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    vi.mocked(toastErr).mockClear();
    installApi();
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("opens the linked JSON resource in the existing read-only preview without navigating to the site root", async () => {
    const originalUrl = window.location.href;
    const dialog = await openSkill();
    expect(fireEvent.click(within(dialog).getByRole("link", { name: "知识点配置" }))).toBe(false);
    const resource = await screen.findByRole("dialog", { name: "产物预览 · knowledge-config.json" });
    expect(within(resource).getByRole("region", { name: "产物源码内容" })).toHaveTextContent('"knowledge_id":"refund"');
    expect(within(resource).queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(config.path)}`);
    expect(window.location.href).toBe(originalUrl);
    expect(toastErr).not.toHaveBeenCalled();
  });

  it("resolves encoded names and parent-relative links against the current artifact directory", async () => {
    const dialog = await openSkill();
    fireEvent.click(within(dialog).getByRole("link", { name: "说明" }));
    const resource = await screen.findByRole("dialog", { name: "产物预览 · 说明.md" });
    expect(within(resource).queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    fireEvent.click(within(resource).getByRole("link", { name: "返回 Skill" }));
    const returned = await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    expect(within(returned).getByRole("button", { name: "编辑产物" })).toBeEnabled();
    expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(guide.path)}`);
  });

  it.each(["不存在", "越界"])("blocks an unresolved %s link without issuing an artifact request or leaving the preview", async (name) => {
    const dialog = await openSkill();
    const calls = mockedApi.mock.calls.length;
    expect(fireEvent.click(within(dialog).getByRole("link", { name }))).toBe(false);
    expect(toastErr).toHaveBeenCalledWith("无法打开产物链接", expect.any(String));
    expect(mockedApi).toHaveBeenCalledTimes(calls);
    expect(dialog).toBeVisible();
  });

  it("asks before discarding unsaved edits when following a resource link in the edit preview", async () => {
    await openSkill();
    fireEvent.click(screen.getByRole("button", { name: "编辑产物" }));
    const editor = screen.getByRole("textbox", { name: "Markdown 正文编辑器" });
    const draft = '[知识点配置](references/knowledge-config.json)\n\n尚未保存的修改。';
    fireEvent.change(editor, { target: { value: draft } });
    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(false);
    const calls = mockedApi.mock.calls.length;
    expect(fireEvent.click(screen.getByRole("link", { name: "知识点配置" }))).toBe(false);
    expect(confirmation).toHaveBeenCalledWith("当前文档有未保存修改，确定放弃修改并打开链接吗？");
    expect(mockedApi).toHaveBeenCalledTimes(calls);
    fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
    expect(screen.getByRole("textbox", { name: "Markdown 正文编辑器" })).toHaveValue(draft);
  });

  it("keeps an archived Skill link inside its own round and its resources read-only", async () => {
    const archivedSkill = { ...skill, path: skill.path.replace("/workspace/", "/round-1/") };
    const archivedConfig = { ...config, path: config.path.replace("/workspace/", "/round-1/") };
    installApi({ ...job, rounds: [
      { round: 1, artifacts: [archivedSkill, archivedConfig] }, { round: 2, artifacts: job.artifacts! },
    ] });
    render(<MiningView active page="jobs" />);
    fireEvent.click(await screen.findByRole("button", { name: /资源链接验收/ }));
    const history = await screen.findByRole("region", { name: "挖掘轮次记录" });
    fireEvent.click(within(history).getByText("第 1 轮 · 2 个产物 · 已归档"));
    fireEvent.click(within(history).getByRole("button", { name: "refund / SKILL.md 只读" }));
    const dialog = await screen.findByRole("dialog", { name: "产物预览 · SKILL.md" });
    fireEvent.click(within(dialog).getByRole("link", { name: "知识点配置" }));
    const resource = await screen.findByRole("dialog", { name: "产物预览 · knowledge-config.json" });
    expect(within(resource).queryByRole("button", { name: "编辑产物" })).not.toBeInTheDocument();
    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith(`/api/mining/artifacts/content?path=${encodeURIComponent(archivedConfig.path)}`));
  });
});
