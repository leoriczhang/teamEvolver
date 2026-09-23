// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import PlatformAssetsView, {
  type PlatformAssetsTreeResponse,
} from "@/views/PlatformAssetsView";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
}));

vi.mock("@/lib/toast", () => ({
  toastErr: vi.fn(),
}));

const mockedApi = vi.mocked(api);

const tree: PlatformAssetsTreeResponse = {
  tenant_id: "tenant-a",
  read_only: true,
  sources: [
    {
      id: "session",
      label: "Session 流水",
      description: "Session 队列与归档",
      backend: "postgres",
      backend_label: "PostgreSQL",
      configured_backend: "postgres",
      root_uri: "platform://session",
      object_count: 1,
      family_count: 1,
      truncated: false,
      families: [
        { key: "sessions", label: "待消费 Session 队列", object_count: 1 },
      ],
    },
    {
      id: "skill",
      label: "Skill 进化产物",
      description: "Candidate 与验证产物",
      backend: "local",
      backend_label: "NAS / 本地存储",
      configured_backend: "local",
      root_uri: "platform://skill",
      object_count: 1,
      family_count: 1,
      truncated: false,
      families: [
        { key: "candidate_skills", label: "Skill Candidate", object_count: 1 },
      ],
    },
  ],
  entries: [
    {
      uri: "platform://session/sessions",
      key: "sessions",
      name: "sessions",
      is_dir: true,
      source_id: "session",
      backend: "postgres",
      relative_path: "sessions",
      purpose: "待消费 Session 队列",
    },
    {
      uri: "platform://session/sessions/session-1.json",
      key: "sessions/session-1.json",
      name: "session-1.json",
      is_dir: false,
      source_id: "session",
      backend: "postgres",
      relative_path: "sessions/session-1.json",
      purpose: "待消费 Session 队列",
    },
    {
      uri: "platform://skill/candidate_skills",
      key: "candidate_skills",
      name: "candidate_skills",
      is_dir: true,
      source_id: "skill",
      backend: "local",
      relative_path: "candidate_skills",
      purpose: "Skill Candidate",
    },
    {
      uri: "platform://skill/candidate_skills/job-1.json",
      key: "candidate_skills/job-1.json",
      name: "job-1.json",
      is_dir: false,
      source_id: "skill",
      backend: "local",
      relative_path: "candidate_skills/job-1.json",
      purpose: "Skill Candidate",
    },
  ],
};

describe("PlatformAssetsView", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/platform-assets/tree") return tree;
      if (path.startsWith("/api/platform-assets/content")) {
        return {
          uri: "platform://skill/candidate_skills/job-1.json",
          key: "candidate_skills/job-1.json",
          name: "job-1.json",
          source_id: "skill",
          source_label: "Skill 进化产物",
          backend: "local",
          backend_label: "NAS / 本地存储",
          purpose: "Skill Candidate",
          size: 40,
          is_text: true,
          content: '{"job_id":"job-1","status":"pending"}',
        };
      }
      throw new Error(`Unexpected API call: ${path}`);
    });
  });

  afterEach(cleanup);

  it("visualizes PostgreSQL and NAS assets without OpenViking CLI", async () => {
    render(<PlatformAssetsView active />);

    expect(await screen.findByText("PostgreSQL")).toBeVisible();
    expect(screen.getAllByText("NAS / 本地存储").length).toBeGreaterThan(0);
    expect(screen.queryByText(/OpenViking CLI/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /job-1\.json/ }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        expect.stringContaining("/api/platform-assets/content"),
      );
    });
    expect(await screen.findByText(/"job_id": "job-1"/)).toBeVisible();
  });

  it("filters the tree by storage partition", async () => {
    render(<PlatformAssetsView active />);
    await screen.findByText("session-1.json");

    fireEvent.click(screen.getByRole("tab", { name: "Skill 产物" }));

    expect(screen.queryByText("session-1.json")).not.toBeInTheDocument();
    expect(screen.getByText("job-1.json")).toBeVisible();
  });
});
