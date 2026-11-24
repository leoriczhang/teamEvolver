// @vitest-environment jsdom
import { api } from "@/api/client";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import TenantKeyViewer from "./TenantKeyViewer";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api).mockResolvedValue({ tenant_id: "alpha", agent_token: "tevt_saved", available: true });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("reads the same saved Key after reopening the page and copies it", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const first = render(<TenantKeyViewer tenantId="alpha" />);
  fireEvent.click(screen.getByRole("button", { name: "查看租户 Key" }));
  const field = await screen.findByLabelText("当前租户 Key");
  expect(field).toHaveValue("tevt_saved");
  expect(field).toHaveAttribute("type", "text");
  fireEvent.click(screen.getByRole("button", { name: "复制" }));
  await screen.findByRole("button", { name: "已复制" });
  expect(writeText).toHaveBeenCalledWith("tevt_saved");
  first.unmount();
  render(<TenantKeyViewer tenantId="alpha" />);
  fireEvent.click(screen.getByRole("button", { name: "查看租户 Key" }));
  expect(await screen.findByLabelText("当前租户 Key")).toHaveValue("tevt_saved");
  expect(api).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api).mock.calls.every(([path, request]) =>
    path === "/api/tenants/alpha/token" && !request?.method)).toBe(true);
});

it("discards a pending response when switching tenants", async () => {
  let resolve!: (value: any) => void;
  vi.mocked(api).mockReturnValue(new Promise(done => { resolve = done; }));
  const view = render(<TenantKeyViewer tenantId="alpha" />);
  fireEvent.click(screen.getByRole("button", { name: "查看租户 Key" }));
  view.rerender(<TenantKeyViewer tenantId="beta" />);
  resolve({ agent_token: "tevt_alpha", available: true });
  await waitFor(() => expect(screen.queryByLabelText("当前租户 Key")).toBeNull());
  expect(screen.queryByDisplayValue("tevt_alpha")).toBeNull();
});
