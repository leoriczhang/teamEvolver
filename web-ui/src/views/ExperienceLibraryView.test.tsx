// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type SkillExperienceListResp } from "@/api/client";
import ExperienceLibraryView from "./ExperienceLibraryView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn() }));

const mockedApi = vi.mocked(api);
const empty: SkillExperienceListResp = {
  items: [], stats: { total_experiences: 0, skills: 0 }, skill_counts: {},
  total: 0, limit: 50, offset: 0, has_more: false,
};

describe("ExperienceLibraryView", () => {
  beforeEach(() => mockedApi.mockReset());
  afterEach(cleanup);

  it("shows named Skill lessons without an uploaded Skill catalogue", async () => {
    mockedApi.mockResolvedValue({
      ...empty,
      total: 1,
      stats: { total_experiences: 1, skills: 1 },
      skill_counts: { "local-only-review": 1 },
      items: [{
        id: "local-skill-1", skill_name: "local-only-review", kind: "exemplary",
        description: "提出审查结论前执行相关验证。", occurrence_count: 2,
        last_ingested_at: "2026-09-20T10:00:00Z",
      }],
    });
    render(<ExperienceLibraryView active />);
    expect(await screen.findByText("提出审查结论前执行相关验证。")).toBeVisible();
    expect(screen.getAllByText(/local-only-review/).length).toBeGreaterThan(0);
    expect(screen.getByText(/最近入库/)).toBeVisible();
    expect(mockedApi).toHaveBeenCalledTimes(1);
    expect(mockedApi).toHaveBeenCalledWith(expect.stringContaining("/api/skill-experiences?"));
  });

  it("explains how to populate an empty library without uploading Skills", async () => {
    mockedApi.mockResolvedValue(empty);
    render(<ExperienceLibraryView active />);
    expect(await screen.findByText(/包含 used_skills 的 Session/)).toBeVisible();
    fireEvent.change(screen.getByPlaceholderText("搜索经验或 Skill"), { target: { value: "missing" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(await screen.findByText("暂无匹配的经验，请调整筛选条件。")).toBeVisible();
  });

  it("keeps loading and retryable failures distinct from an empty library", async () => {
    let reject: (reason: Error) => void = () => {};
    mockedApi.mockReturnValueOnce(new Promise((_resolve, rejectPromise) => { reject = rejectPromise; }));
    render(<ExperienceLibraryView active />);
    expect(screen.getByText("正在加载经验库…")).toBeVisible();
    reject(new Error("storage unavailable"));
    expect(await screen.findByRole("alert")).toHaveTextContent("storage unavailable");
    expect(screen.queryByText(/暂无 Skill 使用经验/)).not.toBeInTheDocument();
    mockedApi.mockResolvedValueOnce(empty);
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText(/包含 used_skills 的 Session/)).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
