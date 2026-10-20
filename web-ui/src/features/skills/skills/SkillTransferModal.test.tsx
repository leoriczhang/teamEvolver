// @vitest-environment jsdom
import { api } from "@/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import SkillTransferModal from "@/features/skills/skills/SkillTransferModal";
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

vi.mock("@/features/skills/api/skillJobs", () => ({ runSkillCommand: vi.fn() }));
vi.mock("@/api/client", () => ({
  api: vi.fn(), getActiveTenantId: () => "tenant-a",
  tenantHeaders: (_path: string, headers: Record<string, string>) => ({ ...headers, "X-TeamEvolver-Tenant": "tenant-a" }),
}));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api).mockResolvedValue({ skills: [{ name: "demo", version: 2 }] });
});

describe("Skill transfer", () => {
  it("submits Git import options and keeps partial failures visible", async () => {
    vi.mocked(runSkillCommand).mockResolvedValue({
      imported: [{ name: "demo", version: 3, status: "updated" }], skipped: [], errors: [{ name: "bad", error: "存储失败" }],
    });
    const imported = vi.fn();
    render(<SkillTransferModal direction="import" onClose={vi.fn()} onImported={imported} />);
    fireEvent.change(screen.getByLabelText("导入来源"), { target: { value: "git" } });
    fireEvent.change(screen.getByLabelText("Git 仓库地址"), { target: { value: "https://git.test/skills.git" } });
    fireEvent.change(screen.getByLabelText("指定 commit（可选）"), { target: { value: "aabbccdd" } });
    fireEvent.click(screen.getByRole("button", { name: "导入 Skill" }));
    await waitFor(() => expect(imported).toHaveBeenCalledOnce());
    expect(JSON.parse(vi.mocked(runSkillCommand).mock.calls[0][1].body as string)).toMatchObject({ channel: "git", conflict: "replace",
      options: { url: "https://git.test/skills.git", commit: "aabbccdd" } });
    expect(vi.mocked(runSkillCommand).mock.calls[0][2]).toBe("/api/skills/transfer");
    expect(screen.getByRole("status")).toHaveTextContent("bad · 存储失败");
  });

  it("exports an explicitly selected Skill to a new repository", async () => {
    vi.mocked(runSkillCommand).mockResolvedValue({ exported: ["demo"], branch: "main" });
    render(<SkillTransferModal direction="export" onClose={vi.fn()} onImported={vi.fn()} />);
    expect(screen.getByRole("button", { name: "导出 Skill" })).toBeDisabled();
    await screen.findByText("demo");
    fireEvent.click(screen.getByRole("checkbox", { name: "demo" }));
    fireEvent.change(screen.getByLabelText("导出目标"), { target: { value: "git" } });
    fireEvent.change(screen.getByLabelText("Git 上传方式"), { target: { value: "new_repository" } });
    fireEvent.change(screen.getByLabelText("新仓库名称"), { target: { value: "my-skills" } });
    fireEvent.click(screen.getByRole("button", { name: "导出 Skill" }));
    await waitFor(() => expect(runSkillCommand).toHaveBeenCalledOnce());
    expect(JSON.parse(vi.mocked(runSkillCommand).mock.calls[0][1].body as string)).toMatchObject({ names: ["demo"], channel: "git",
      options: { mode: "new_repository", repo_name: "my-skills" } });
    expect(await screen.findByRole("status")).toHaveTextContent("已导出 1 个 Skill");
  });
});
