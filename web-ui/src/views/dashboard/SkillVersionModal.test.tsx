// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import SkillVersionModal from "./SkillVersionModal";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/components/UnifiedDiffView", () => ({ default: () => null }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("renders unavailable metrics in published version history without NaN or false ties", async () => {
  vi.mocked(api).mockResolvedValue({ version: 1, current_version: 1, evolution: { evaluation: { replay: { efficiency: { dimensions: {
    total_tokens: { baseline: "unavailable", candidate: null, delta: "unavailable", winner: "unavailable", reduction_ratio: null },
    tool_call_count: { baseline: 0, candidate: 0, delta: 0, winner: "tie", reduction_ratio: 0 },
  } } } } } });
  render(<SkillVersionModal name="mined-skill" initialVersion={1} open onClose={vi.fn()} onRolled={vi.fn()} />);
  const row = (await screen.findByText("Tokens")).closest("tr")!;
  expect(within(row).getAllByText("未观测")).toHaveLength(4);
  expect(row).not.toHaveTextContent(/NaN|持平/);
  const zero = screen.getByText("工具调用").closest("tr")!;
  expect(within(zero).getAllByText("0")).toHaveLength(3);
  expect(within(zero).getByText("持平")).toBeVisible();
});
