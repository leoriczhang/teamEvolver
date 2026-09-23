// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import SkillWorkbenchView from "@/views/SkillWorkbenchView";

vi.mock("@/api/client", () => ({ api: vi.fn(), cloudNote: () => "", getActiveTenantId: () => "" }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));
vi.mock("@/views/workbench/CodeEditor", () => ({
  languageName: () => "Markdown",
  default: ({ path, value, onChange }: { path: string; value: string; onChange: (value: string) => void }) => <textarea aria-label={`代码编辑器 ${path}`} value={value} onChange={(event) => onChange(event.target.value)} />,
}));
const mockApi = vi.mocked(api);
const md = "---\nname: demo\ndescription: Example\n---\nOriginal";
const index = { root: "/skills", name: "skills", skills: [{ name: "demo", relative_path: "demo", files: ["SKILL.md", "scripts/run.py"] }] };
const user = { id: "alice", display_name: "Alice", role: "admin" as const };
let runBody: any;

async function mockRequest(path: string, init?: RequestInit): Promise<any> {
  if (path === "/api/replay-lab/workspace") return index;
  if (path.startsWith("/api/replay-lab/workspace/demo/file")) {
    const file = new URL(path, "http://test").searchParams.get("path");
    return { path: file, editable: true, content: file === "SKILL.md" ? md : "print('old')", sha256: "original-hash" };
  }
  if (path === "/api/replay-lab/skills") return { skills: index.skills };
  if (path === "/api/replay-lab/skills/demo") return { name: "demo", skill_md: md };
  if (path.startsWith("/api/replay-lab/datasets?")) return { datasets: [{ dataset_id: "ds-1", skill_name: "demo", name: "测试数据集", query: "Run the skill", requirements: "1. Complete" }] };
  if (path.startsWith("/api/replay-lab/runs?")) return { runs: [] };
  if (path === "/api/replay-lab/runs" && init?.method === "POST") {
    runBody = JSON.parse(String(init.body));
    return { run_id: "r1", dataset_id: "ds-1", skill_name: "demo", status: "running" };
  }
  throw new Error(`Unexpected request: ${path}`);
}

async function openSkill() {
  fireEvent.click(await screen.findByRole("button", { name: "打开第一个 SKILL.md" }));
  await screen.findByRole("textbox", { name: "代码编辑器 SKILL.md" });
  await screen.findByRole("option", { name: /测试数据集/ });
}

beforeEach(() => { mockApi.mockReset(); mockApi.mockImplementation(mockRequest); runBody = undefined; window.localStorage?.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("Skill code workbench", () => {
  it("offers ZIP and Git import when the real code folder is empty", async () => {
    mockApi.mockImplementation((path, init) => path === "/api/replay-lab/workspace"
      ? Promise.resolve({ ...index, skills: [] }) : mockRequest(path, init));
    render(<SkillWorkbenchView active user={user} />);
    fireEvent.click(await screen.findByRole("button", { name: "导入 Skill 代码" }));
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(screen.getByRole("option", { name: "Git 仓库同步" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "ZIP 包上传" })).toBeInTheDocument();
    expect(screen.queryByText("Skill Lab")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory Lab")).not.toBeInTheDocument();
    expect(screen.queryByText("团队与个人资产")).not.toBeInTheDocument();
  });

  it("keeps per-file edits and runs the full unsaved Candidate", async () => {
    render(<SkillWorkbenchView active user={user} />);
    await openSkill();
    fireEvent.change(screen.getByRole("textbox", { name: "代码编辑器 SKILL.md" }), { target: { value: md + "\nCandidate" } });
    fireEvent.click(screen.getByRole("button", { name: "scripts" }));
    fireEvent.click(screen.getByRole("button", { name: "run.py" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "代码编辑器 scripts/run.py" }), { target: { value: "print('candidate')" } });
    fireEvent.click(screen.getByRole("tab", { name: "SKILL.md" }));
    expect(screen.getByRole("textbox", { name: "代码编辑器 SKILL.md" })).toHaveValue(md + "\nCandidate");
    fireEvent.click(screen.getByRole("button", { name: "运行 True Replay" }));
    await waitFor(() => expect(runBody).toMatchObject({ skill_name: "demo", dataset_id: "ds-1", candidate_skill_md: md + "\nCandidate", candidate_files: { "scripts/run.py": "print('candidate')" } }));
    expect(screen.getByRole("region", { name: "调试控制台" })).toBeVisible();
    expect(mockApi.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  });

  it("preserves typing made while a save is in flight", async () => {
    let resolveSave!: (value: unknown) => void;
    mockApi.mockImplementation((path, init) => init?.method === "PUT" ? new Promise((resolve) => { resolveSave = resolve; }) : mockRequest(path, init));
    render(<SkillWorkbenchView active user={user} />);
    await openSkill();
    const editor = screen.getByRole("textbox", { name: "代码编辑器 SKILL.md" });
    fireEvent.change(editor, { target: { value: md + "\nSaved" } });
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    fireEvent.change(editor, { target: { value: md + "\nTyped during save" } });
    await act(async () => resolveSave({ content: md + "\nSaved", sha256: "saved-hash", editable: true }));
    expect(editor).toHaveValue(md + "\nTyped during save");
    expect(screen.getByText("1 个文件未保存")).toBeVisible();
    const saveCall = mockApi.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(saveCall?.[1]?.body))).toMatchObject({ expected_sha256: "original-hash", content: md + "\nSaved" });
  });

  it("does not discard a dirty file when closing is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SkillWorkbenchView active user={user} />);
    await openSkill();
    fireEvent.change(screen.getByRole("textbox", { name: "代码编辑器 SKILL.md" }), { target: { value: "Unsaved" } });
    fireEvent.click(screen.getByRole("button", { name: "关闭 demo/SKILL.md" }));
    expect(screen.getByRole("textbox", { name: "代码编辑器 SKILL.md" })).toHaveValue("Unsaved");
  });

  it("adds a nested file to the Candidate without writing to the library", async () => {
    render(<SkillWorkbenchView active user={user} />);
    await openSkill();
    fireEvent.click(screen.getByRole("button", { name: "新建文件" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新文件相对路径" }), { target: { value: "references/check.md" } });
    fireEvent.click(screen.getByRole("button", { name: "创建文件" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "代码编辑器 references/check.md" }), { target: { value: "New reference" } });
    fireEvent.click(screen.getByRole("button", { name: "运行 True Replay" }));
    await waitFor(() => expect(runBody?.candidate_files).toEqual({ "references/check.md": "New reference" }));
    expect(runBody.candidate_skill_md).toBe(md);
  });

  it("opens files through Ctrl+P without an OpenViking connection", async () => {
    render(<SkillWorkbenchView active user={user} />);
    await screen.findByRole("button", { name: "打开第一个 SKILL.md" });
    fireEvent.keyDown(window, { key: "p", ctrlKey: true });
    const search = screen.getByRole("textbox", { name: "快速打开文件搜索" });
    fireEvent.change(search, { target: { value: "run.py" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(await screen.findByRole("textbox", { name: "代码编辑器 scripts/run.py" })).toHaveValue("print('old')");
    expect(mockApi.mock.calls.some(([path]) => path.includes("openviking"))).toBe(false);
  });
});
