// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import type { ExecutionProfile } from "@/features/skills/api/client";
import ExecutionReleasePanel from "./ExecutionReleasePanel";
import { toastErr } from "@/lib/toast";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const profile: ExecutionProfile = {
  profile_id: "business:profile", runtime_type: "observed_http", agent_id: "agent-one", user_id: "alice",
  affected_skill_ids: ["report", "review"], environment: {}, revision: 2,
  current_release_id: "release-new", composition_hash: "new-hash", execution_plan: { components: [] },
  releases: [
    { release_id: "release-new", composition_hash: "new-hash" },
    { release_id: "release-old", composition_hash: "old-hash" },
  ],
  receipts: [
    { request_id: "receipt-one", release_id: "release-old", composition_hash: "old-hash", state: "downloaded", evidence_level: "agent_reported" },
    { request_id: "receipt-two", release_id: "release-new", composition_hash: "new-hash", state: "activated", evidence_level: "agent_reported" },
  ],
};

describe("ExecutionReleasePanel", () => {
  it("loads immutable releases and Agent reports on request and exposes no rollback to a non-admin", async () => {
    vi.mocked(api).mockResolvedValue(profile);
    const onUpdated = vi.fn();
    render(<ExecutionReleasePanel profileId={profile.profile_id} isAdmin={false} onUpdated={onUpdated} />);
    expect(api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "查看发布与 Agent 回执" }));
    expect(await screen.findByText("当前组合发布：release-new")).toBeVisible();
    expect(api).toHaveBeenCalledWith("/api/skill-evolution/execution-profiles/business%3Aprofile");
    expect(onUpdated).toHaveBeenCalledWith(profile);
    expect(screen.getByText("已下载 · agent_reported")).toBeVisible();
    expect(screen.getByText("已激活 · agent_reported")).toBeVisible();
    expect(screen.getByText(/不证明模型已经实际使用/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /回滚到此组合/ })).not.toBeInTheDocument();
  });

  it("confirms the whole target composition and freezes the reviewed current release in the background command", async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => options?.method === "POST"
      ? { job_id: "rollback:task-one" } : profile);
    render(<ExecutionReleasePanel profileId={profile.profile_id} isAdmin onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "查看发布与 Agent 回执" }));
    fireEvent.click(await screen.findByRole("button", { name: "回滚到此组合 · release-old" }));
    expect(screen.getByText(/将整套 Skill／业务 Prompt／上下文组合恢复到：release-old/)).toBeVisible();
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "确认整套组合回滚" }));
    expect(await screen.findByRole("status")).toHaveTextContent("rollback:task-one · queued");
    expect(screen.getByText(/受理成功不代表已经生效/)).toBeVisible();
    const post = vi.mocked(api).mock.calls.find(([, options]) => options?.method === "POST");
    expect(post?.[0]).toBe("/api/skill-evolution/execution-profiles/business%3Aprofile/rollback");
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({ release_id: "release-old", expected_release_id: "release-new" });
    expect(screen.getByRole("button", { name: "回滚到此组合 · release-old" })).toBeDisabled();

    vi.mocked(api).mockImplementation(async path => path.startsWith("/api/jobs/")
      ? { status: "failed", error_code: "COMPOSITION_CHANGED" } : profile);
    fireEvent.click(screen.getByRole("button", { name: "刷新发布与 Agent 回执" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("failed · COMPOSITION_CHANGED"));
    expect(api).toHaveBeenCalledWith("/api/jobs/rollback%3Atask-one");
    expect(screen.getByRole("button", { name: "回滚到此组合 · release-old" })).toBeEnabled();
  });

  it("keeps a rejected rollback visible without claiming activation", async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => {
      if (options?.method === "POST") throw new Error("execution baseline changed");
      return profile;
    });
    render(<ExecutionReleasePanel profileId={profile.profile_id} isAdmin onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "查看发布与 Agent 回执" }));
    fireEvent.click(await screen.findByRole("button", { name: "回滚到此组合 · release-old" }));
    fireEvent.click(screen.getByRole("button", { name: "确认整套组合回滚" }));
    await waitFor(() => expect(toastErr).toHaveBeenCalledWith("提交组合回滚失败", "execution baseline changed"));
    expect(screen.getByRole("button", { name: "确认整套组合回滚" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
