import { describe,expect,it } from "vitest";

import { hasOpenVikingConfiguration } from "@/lib/sharing";

describe("hasOpenVikingConfiguration", () => {
  it("accepts a configured endpoint and credential", () => {
    expect(
      hasOpenVikingConfiguration({
        endpoint: "http://openviking.test:1933",
        api_key_present: true,
      }),
    ).toBe(true);
  });

  it("still requires an endpoint and credential", () => {
    expect(hasOpenVikingConfiguration({ api_key_present: true })).toBe(false);
    expect(
      hasOpenVikingConfiguration({ endpoint: "http://openviking.test:1933" }),
    ).toBe(false);
  });
});
