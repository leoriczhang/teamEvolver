// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,expect,it,vi } from "vitest";

import { api } from "@/api/client";
import { type UserProfile } from "@/features/admin/api/client";
import UsersView from "@/features/admin/UsersView";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("lets a member save their profile without calling the platform access API", async () => {
  const user: UserProfile = {
    id: "alice",
    display_name: "Alice",
    email: "alice@example.com",
    role: "user",
    platform_role: "user",
    tenant_role: "member",
    tenant_id: "tenant-a",
    status: "active",
    personal_space: { backend: "viking", viking_user: "alice" },
    team_space: { backend: "viking", viking_user: "team", bound: true },
  };
  vi.mocked(api).mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === "/api/users") {
      return {
        users: [user],
        openviking_account: "tenant-a",
        default_team_user: "team",
      };
    }
    if (path === "/api/team-settings") {
      return { display_name: "Team", configured_display_name: "Team" };
    }
    if (path === "/api/users/alice/profile" && options?.method === "PUT") {
      return { ...user, display_name: "Alice Updated" };
    }
    throw new Error(`unexpected API call: ${path}`);
  });

  render(<UsersView active user={user} />);
  const displayName = await screen.findByDisplayValue("Alice");
  fireEvent.change(displayName, { target: { value: "Alice Updated" } });
  fireEvent.click(screen.getByRole("button", { name: "保存用户" }));

  await waitFor(() => {
    expect(api).toHaveBeenCalledWith(
      "/api/users/alice/profile",
      expect.objectContaining({ method: "PUT" }),
    );
  });
  expect(vi.mocked(api).mock.calls.some(([path]) =>
    String(path).includes("/api/identity/users/")
  )).toBe(false);
});
