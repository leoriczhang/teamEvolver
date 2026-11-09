import { describe, expect, it } from "vitest";
import type { ExecutionComponent, ExecutionPlan } from "@/features/skills/api/client";
import { executionObjects } from "./variableGroups";

const member = (id: string, name: string, value: string, host = "host"): ExecutionComponent => ({
  component_id: id, kind: "context_fragment", slot: "context", order: 1, revision: "1",
  content: value, content_hash: `body-${value}`,
  binding: { inline_variable: { component_id: host, name, source_value_hash: `source-${value}` } },
});

describe("variable evolution objects", () => {
  it("groups by placeholder across original values and hosts, never by multi-variable combinations", () => {
    const plan: ExecutionPlan = { binding: { profile_id: "one-profile" }, components: [
      member("a1", "rules", "a"), member("b1", "examples", "b"),
      member("a2", "rules", "c"), member("b2", "examples", "d"), member("a3", "rules", "a", "another-host"),
    ] };
    const original = JSON.stringify(plan);
    const objects = executionObjects(plan);
    expect(objects.map(object => [object.id, object.valueCount, object.members.map(part => part.component_id)])).toEqual([
      ["variable-group:one-profile:rules", 2, ["a1", "a2", "a3"]],
      ["variable-group:one-profile:examples", 2, ["b1", "b2"]],
    ]);
    expect(objects[0].members[2].binding?.inline_variable?.component_id).toBe("another-host");
    expect(JSON.stringify(plan)).toBe(original);
    expect(executionObjects({ ...plan, environment: { template_variables: { rules: "c", examples: "b" } } })).toEqual(objects);
  });

  it("keeps ordinary Prompt and fixed Context objects unchanged", () => {
    const context = member("fixed-context", "unused", "a");
    delete context.binding;
    expect(executionObjects({ components: [context] })).toEqual([
      { id: "fixed-context", valueCount: 0, members: [context] },
    ]);
  });
});
