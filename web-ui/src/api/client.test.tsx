// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn() }));
afterEach(() => vi.unstubAllGlobals());
it.each([
  [[{ loc: ["body", "subject"], msg: "Field required", input: "secret source", ctx: { secret: "hidden" } }], "body.subject: Field required"],
  [{ code: "FORBIDDEN", message: "Access denied", input: "secret source" }, "Access denied"],
  ["ONTOLOGY_DISABLED", "ONTOLOGY_DISABLED"],
])("renders structured error details without echoing the submitted body", async (detail, expected) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail }), { status: 422 })));
  const error = await api("/te/enterprise/v1/access").catch(e => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error.message).toBe(expected);
  expect(error.status).toBe(422);
  expect(error.message).not.toContain("secret source");
  expect(error.message).not.toContain("[object Object]");
});
