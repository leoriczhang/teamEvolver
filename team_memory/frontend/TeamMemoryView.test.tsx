// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TeamMemoryView from "./TeamMemoryView";
import { api } from "@/api/client";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastOk: vi.fn(), toastErr: vi.fn() }));
const mock = vi.mocked(api);
const settings = {
  target_root: "viking://resources/shared-knowledge", okf_skill_uri: "viking://agent/skills/aggregate",
  maintenance_skill_uri: "viking://agent/skills/maintain",
};
beforeEach(() => {
  mock.mockReset();
  mock.mockImplementation(async (path, options) => {
    if (path === "/api/aggregation/settings") return settings;
    if (path === "/api/aggregation/runs") return { runs: [] };
    if (path === "/api/aggregation/users") return { users: ["alice", "bob"] };
    if (path.startsWith("/api/aggregation/okf-skill")) {
      const stage = new URL(path, "http://local").searchParams.get("stage");
      return { body: options?.method === "PUT" ? JSON.parse(String(options.body)).body : `${stage} body`, skill_uri: stage };
    }
    if (path === "/api/aggregation/run") return { task_id: "run-1", status: "completed", stage: "completed", groups: [] };
    throw new Error(path);
  });
});
afterEach(cleanup);

describe("TeamMemoryView", () => {
  it("edits the maintenance Skill independently", async () => {
    render(<TeamMemoryView active user={{ id: "admin", role: "admin" }} />);
    await screen.findByDisplayValue("maintenance body");
    fireEvent.change(screen.getByLabelText("DreamCycle Skill内容"), { target: { value: "maintenance revised" } });
    fireEvent.click(screen.getByLabelText("保存DreamCycle Skill"));
    await waitFor(() => expect(mock).toHaveBeenCalledWith(
      expect.stringContaining("stage=maintenance"), expect.objectContaining({ method: "PUT", body: expect.stringContaining("maintenance revised") }),
    ));
    expect(screen.getByLabelText("聚合 Skill内容")).toHaveValue("aggregation body");
  });

  it("runs both stages for selected users and maintenance without member selection", async () => {
    render(<TeamMemoryView active user={{ id: "admin", role: "admin" }} />);
    await screen.findByDisplayValue(settings.target_root);
    expect(screen.getByRole("button", { name: "运行" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "加载成员" }));
    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: "运行" }));
    await waitFor(() => expect(mock).toHaveBeenCalledWith("/api/aggregation/run", expect.objectContaining({ body: expect.stringContaining('"pipeline":"both"') })));
    fireEvent.click(screen.getByRole("button", { name: "仅 DreamCycle" }));
    fireEvent.click(screen.getByRole("button", { name: "运行" }));
    await waitFor(() => expect(mock).toHaveBeenCalledWith("/api/aggregation/run", expect.objectContaining({ body: expect.stringContaining('"pipeline":"maintain"') })));
  });

  it("does not fetch privileged data for non-admins", () => {
    render(<TeamMemoryView active user={{ id: "reader", role: "user" }} />);
    expect(mock).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("DreamCycle Skill内容")).not.toBeInTheDocument();
  });
});
