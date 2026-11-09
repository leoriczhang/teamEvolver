// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ExecutionPlan } from "@/features/skills/api/client";
import ExecutionVersionContent from "./ExecutionVersionContent";

afterEach(cleanup);
const baseline: ExecutionPlan = { components: [{
  component_id: "stable-context", kind: "context_fragment", slot: "context", order: 1, revision: "1",
  content_hash: "old-content", content: "Original rule",
  binding: { inline_variable: { component_id: "host-prompt", name: "rules", source_value_hash: "source-original" } },
}] };

describe("ExecutionVersionContent variable assets", () => {
  it("shows the original template position and immutable source selector across content revisions", () => {
    const plan: ExecutionPlan = { components: [{ ...baseline.components[0], revision: "2", content_hash: "new-content", content: "Improved rule" }] };
    const view = render(<ExecutionVersionContent plan={plan} baseline={baseline} showDiff={false} />);
    expect(screen.getByRole("option", { name: "变量组 rules · 1 个取值" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "stable-context · v2 · 原值 source-origi" })).toBeInTheDocument();
    expect(screen.getByText("注入位置：host-prompt 中的 {{rules}}")).toBeVisible();
    expect(screen.getByText("原值哈希：source-original")).toBeVisible();
    expect(screen.getByText("Improved rule")).toBeVisible();
    fireEvent.change(screen.getByLabelText("组内取值成员"), { target: { value: "stable-context" } });
    view.rerender(<ExecutionVersionContent plan={plan} baseline={baseline} showDiff />);
    expect(screen.getByText("对照组件：v1 · 正文已变化")).toBeVisible();
    expect(screen.getByText("Original rule")).toBeVisible();
    expect(screen.getByText("Improved rule")).toBeVisible();
    expect(screen.getByText("原值哈希：source-original")).toBeVisible();
  });

  it("selects a variable group before one precise original-value member", () => {
    const second = { ...baseline.components[0], component_id: "other-value", content: "Other rule",
      binding: { inline_variable: { component_id: "other-host", name: "rules", source_value_hash: "other-original" } } };
    render(<ExecutionVersionContent plan={{ ...baseline, components: [...baseline.components, second] }} showDiff={false} />);
    expect(screen.getByLabelText("业务组件").querySelectorAll("option")).toHaveLength(1);
    expect(screen.getByRole("option", { name: "变量组 rules · 2 个取值" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("组内取值成员"), { target: { value: "other-value" } });
    expect(screen.getByText("Other rule")).toBeVisible();
    expect(screen.getByText("注入位置：other-host 中的 {{rules}}")).toBeVisible();
    expect(screen.getByText("原值哈希：other-original")).toBeVisible();
  });
});
