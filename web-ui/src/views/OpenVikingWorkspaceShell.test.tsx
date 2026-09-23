// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import OpenVikingWorkspaceShell, {
  type ScopeConfig,
  type ScopeName,
  type TreeResponse,
  type WorkspaceConfig,
} from "@/views/OpenVikingWorkspaceShell";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  getActiveTenantId: () => "",
}));

vi.mock("@/lib/toast", () => ({
  toastErr: vi.fn(),
  toastOk: vi.fn(),
}));

const mockedApi = vi.mocked(api);

function scope(
  name: ScopeName,
  rootUri: string,
  space: "personal" | "team",
  kind: ScopeConfig["kind"],
): ScopeConfig {
  return {
    name,
    root_uri: rootUri,
    space,
    kind,
    can_write: true,
  };
}

const config: WorkspaceConfig = {
  enabled: true,
  deployment: "local",
  endpoint: "http://openviking.test",
  account: "account-a",
  root_key_configured: true,
  user_id: "alice",
  scopes: {
    personal_memory: scope(
      "personal_memory",
      "viking://user/alice/memories",
      "personal",
      "memory",
    ),
    personal_skills: scope(
      "personal_skills",
      "viking://resources/team/peers/alice/skills",
      "personal",
      "skills",
    ),
    personal_resources: scope(
      "personal_resources",
      "viking://user/alice/resources",
      "personal",
      "resources",
    ),
    team_memory: scope(
      "team_memory",
      "viking://resources/shared-knowledge",
      "team",
      "memory",
    ),
    team_skills: scope(
      "team_skills",
      "viking://resources/team/skills",
      "team",
      "skills",
    ),
    team_resources: scope(
      "team_resources",
      "viking://resources/team",
      "team",
      "resources",
    ),
    personal_workspace: scope(
      "personal_workspace",
      "viking://user",
      "personal",
      "workspace",
    ),
    team_workspace: scope(
      "team_workspace",
      "viking://resources",
      "team",
      "workspace",
    ),
  } as WorkspaceConfig["scopes"],
};

function treeResponse(scopeName: ScopeName, entries: TreeResponse["entries"]): TreeResponse {
  const selected = config.scopes[scopeName];
  return {
    scope: scopeName,
    root_uri: selected.root_uri,
    uri: selected.root_uri,
    entries,
    exists: true,
    can_write: selected.can_write,
  };
}

describe("OpenVikingWorkspaceShell", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    window.localStorage.clear();
    Element.prototype.scrollTo = vi.fn();
  });
  afterEach(cleanup);

  it("exposes only personal memory and team resources at namespace roots", async () => {
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/users") {
        return { users: [{ id: "alice", display_name: "Alice", role: "admin" }] };
      }
      if (path.startsWith("/api/openviking/workspace/config")) return config;
      if (path.startsWith("/api/openviking/workspace/tree")) {
        const scopeName = new URL(path, "http://localhost").searchParams.get("scope") as ScopeName;
        return treeResponse(scopeName, []);
      }
      throw new Error(`Unexpected API call: ${path}`);
    });

    render(
      <OpenVikingWorkspaceShell
        active
        user={{ id: "alice", display_name: "Alice", role: "admin" }}
      />,
    );

    expect(await screen.findByRole("button", { name: "个人记忆" })).toBeVisible();
    expect(screen.getAllByText("viking://user").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "团队资源" }));
    await waitFor(() => expect(screen.getAllByText("viking://resources").length).toBeGreaterThan(0));
    expect(screen.queryByText("Skill Lab")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory Lab")).not.toBeInTheDocument();
  });


  it("keeps team memory visible when the initial personal-space request finishes late", async () => {
    let releasePersonalRequests!: () => void;
    const personalRequestsBlocked = new Promise<void>((resolve) => {
      releasePersonalRequests = resolve;
    });

    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/users") {
        return { users: [{ id: "alice", display_name: "Alice", role: "admin" }] };
      }
      if (path.startsWith("/api/openviking/workspace/config")) {
        return config;
      }
      if (path.startsWith("/api/openviking/workspace/tree")) {
        const scopeName = new URL(path, "http://localhost").searchParams.get("scope") as ScopeName;
        if (scopeName.startsWith("personal_")) {
          await personalRequestsBlocked;
          return treeResponse(scopeName, []);
        }
        if (scopeName === "team_workspace") {
          return treeResponse(scopeName, [
            {
              uri: "viking://resources/team-memory.md",
              name: "team-memory.md",
              is_dir: false,
            },
          ]);
        }
        return treeResponse(scopeName, []);
      }
      throw new Error(`Unexpected API call: ${path}`);
    });

    render(
      <OpenVikingWorkspaceShell
        active
        user={{ id: "alice", display_name: "Alice", role: "admin" }}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "团队资源" }));
    await screen.findByText("team-memory.md");

    await act(async () => {
      releasePersonalRequests();
      await personalRequestsBlocked;
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    await waitFor(() => {
      expect(screen.getByText("team-memory.md")).toBeInTheDocument();
    });
  });
});
