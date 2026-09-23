import { webcrypto } from "node:crypto";
import { afterEach, expect, it, vi } from "vitest";
import { createRequestId } from "./requestId";

afterEach(() => vi.unstubAllGlobals());

it("uses the native UUID API when available", () => {
  const randomUUID = vi.fn(() => "ac1459b3-8f92-4f32-a3b2-83c4ca283d03");
  vi.stubGlobal("crypto", { randomUUID });
  expect(createRequestId()).toBe("ac1459b3-8f92-4f32-a3b2-83c4ca283d03");
  expect(randomUUID).toHaveBeenCalledOnce();
});

it("generates distinct UUID v4 keys without the secure-context-only API", () => {
  vi.stubGlobal("crypto", { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) });
  const keys = Array.from({ length: 100 }, createRequestId);
  expect(new Set(keys).size).toBe(100);
  for (const key of keys) expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

it.each([undefined, {}])("reports unavailable randomness explicitly: %s", crypto => {
  vi.stubGlobal("crypto", crypto);
  expect(createRequestId).toThrow("BROWSER_RANDOM_UNAVAILABLE");
});
