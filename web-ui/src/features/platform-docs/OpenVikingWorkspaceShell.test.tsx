// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act,cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

import { api } from "@/api/client";
import {
type ScopeConfig,
type ScopeName,
type TreeResponse,
type WorkspaceConfig,
} from "@/features/platform-docs/OpenVikingWorkspaceShellShared";
import OpenVikingWorkspaceShell from "./OpenVikingWorkspaceShell";

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

  it.each([
    { library: "memory" as const, personal: "个人记忆", team: "团队记忆",
      personalScope: "personal_memory", teamScope: "team_memory" },
    { library: "resources" as const, personal: "个人资源", team: "团队资源",
      personalScope: "personal_resources", teamScope: "team_resources" },
  ])("loads only the selected library's personal and team scopes: $library", async ({
    library, personal, team, personalScope, teamScope,
  }) => {
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
        library={library}
        user={{ id: "service-root", display_name: "Root", role: "admin" }}
      />,
    );

    expect(await screen.findByRole("button", { name: personal })).toBeVisible();
    await waitFor(() => expect(screen.getAllByText(config.scopes[personalScope as ScopeName].root_uri).length)
      .toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("button", { name: team }));
    await waitFor(() => expect(screen.getAllByText(config.scopes[teamScope as ScopeName].root_uri).length)
      .toBeGreaterThan(0));
    const requestedScopes = mockedApi.mock.calls.map(([path]) => String(path))
      .filter(path => path.startsWith("/api/openviking/workspace/tree"))
      .map(path => new URL(path, "http://localhost").searchParams.get("scope"));
    expect(new Set(requestedScopes)).toEqual(new Set([personalScope, teamScope]));
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
        if (scopeName === "team_memory") {
          return treeResponse(scopeName, [
            {
              uri: "viking://resources/shared-knowledge/team-memory.md",
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

    fireEvent.click(await screen.findByRole("button", { name: "团队记忆" }));
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
