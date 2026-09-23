// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TenantSwitcher } from "@/App";

afterEach(cleanup);

describe("customer project switcher", () => {
  it("searches names, hides disabled accounts and selects the account ID", () => {
    const onSelect = vi.fn();
    render(<TenantSwitcher projectMode activeTenantId="a" onSelect={onSelect} tenants={[
      { tenant_id: "a", display_name: "Product", status: "active" },
      { tenant_id: "b", display_name: "Dispatch", status: "active" },
      { tenant_id: "c", display_name: "Disabled", status: "disabled" },
    ]} />);
    fireEvent.click(screen.getByRole("button", { name: /项目切换/ }));
    expect(screen.queryByRole("menuitem", { name: /Disabled/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目" }), { target: { value: "Dispa" } });
    expect(screen.queryByRole("menuitem", { name: /Product/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: /Dispatch/ }));
    expect(onSelect).toHaveBeenCalledWith("b");
  });

  it("shows an empty search state", () => {
    render(<TenantSwitcher projectMode activeTenantId="a" onSelect={() => {}} tenants={[
      { tenant_id: "a", display_name: "Product", status: "active" },
    ]} />);
    fireEvent.click(screen.getByRole("button", { name: /项目切换/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目" }), { target: { value: "no-match" } });
    expect(screen.getByRole("status")).toHaveTextContent("无匹配项目");
  });
});
