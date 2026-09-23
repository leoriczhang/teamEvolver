// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import EvolutionWorkspaceView from "@/views/EvolutionWorkspaceView";

vi.mock("@/views/PromptStudioView", () => ({
  default: () => <div>skills-evolution-content</div>,
}));

vi.mock("@memory/TeamMemoryView", () => ({
  default: () => <div>memory-evolution-content</div>,
}));

afterEach(cleanup);

describe("EvolutionWorkspaceView", () => {
  it("hides team Memory evolution until OpenViking is configured", () => {
    render(
      <EvolutionWorkspaceView
        active
        openVikingConfigured={false}
        user={{ id: "admin", role: "admin" }}
      />,
    );

    expect(screen.getByRole("tab", { name: "Skills 自进化" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "团队 Memory 自进化" })).not.toBeInTheDocument();
    expect(screen.queryByText("memory-evolution-content")).not.toBeInTheDocument();
  });

  it("shows team Memory evolution after OpenViking is configured", () => {
    render(
      <EvolutionWorkspaceView
        active
        openVikingConfigured
        user={{ id: "admin", role: "admin" }}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "团队 Memory 自进化" }));
    expect(screen.getByText("memory-evolution-content")).toBeVisible();
  });
});
