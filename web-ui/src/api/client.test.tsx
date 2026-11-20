// @vitest-environment jsdom
import { afterEach,expect,it,vi } from "vitest";
import { api,ApiError,setActiveTenantId } from "./client";
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn() }));
afterEach(() => vi.unstubAllGlobals());
it("clears a connection belonging to the old tenant while preserving a shared link in the same tenant", () => {
  window.history.replaceState(null, "", "/?tenant=video&connection_id=video-ma");
  setActiveTenantId("video");
  expect(new URLSearchParams(window.location.search).get("connection_id")).toBe("video-ma");
  setActiveTenantId("notes");
  expect(new URLSearchParams(window.location.search).get("tenant")).toBe("notes");
  expect(new URLSearchParams(window.location.search).has("connection_id")).toBe(false);
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});
it.each([
  [[{ loc: ["body", "subject"], msg: "Field required", input: "secret source", ctx: { secret: "hidden" } }], "body.subject: Field required"],
  [{ code: "FORBIDDEN", message: "Access denied", input: "secret source" }, "Access denied"],
  ["TENANT_CONFIG_MISSING", "TENANT_CONFIG_MISSING"],
])("renders structured error details without echoing the submitted body", async (detail, expected) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail }), { status: 422 })));
  const error = await api("/api/skills").catch(e => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error.message).toBe(expected);
  expect(error.status).toBe(422);
  expect(error.message).not.toContain("secret source");
  expect(error.message).not.toContain("[object Object]");
});

it("rechecks authentication without reloading the current view on login-required responses", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(
    JSON.stringify({ detail: "login required" }),
    { status: 401 },
  )));
  const authChanged = vi.fn();
  window.addEventListener("console-auth-changed", authChanged);

  await expect(api("/api/model-settings")).rejects.toMatchObject({
    message: "login required",
    status: 401,
  });
  expect(authChanged).toHaveBeenCalledOnce();

  window.removeEventListener("console-auth-changed", authChanged);
});

it("waits for the cloud Skill receipt before returning a saved version", async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      status: "accepted", job_id: "ma-skill:edit", version: 2, result_url: "/api/ma/skill-jobs/ma-skill:edit",
    }), { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      status: "succeeded", result: { status: "published", version: 3, readback_verified: true },
    })));
  vi.stubGlobal("fetch", fetch);
  await expect(api("/api/skills", { method: "POST" })).resolves.toMatchObject({
    status: "published", version: 3, readback_verified: true,
  });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("does not turn a failed cloud Skill command into save success", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      status: "accepted", job_id: "ma-skill:edit", result_url: "/api/ma/skill-jobs/ma-skill:edit",
    }), { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      status: "failed", error: "Cloud Skill changed after acceptance",
    }))));
  await expect(api("/api/skills", { method: "POST" })).rejects.toThrow("Cloud Skill changed after acceptance");
});
