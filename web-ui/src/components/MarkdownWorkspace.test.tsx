// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkdownDocument } from "@/components/MarkdownWorkspace";

afterEach(cleanup);

describe("MarkdownDocument links", () => {
  it("adds stable ids to headings and scrolls same-document anchors", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { container } = render(
      <MarkdownDocument content={"# 安装说明\n\n[跳到参数](#参数设置)\n\n## 参数设置"} />,
    );

    expect(container.querySelector("h1")).toHaveAttribute("id", "安装说明");
    expect(container.querySelector("h2")).toHaveAttribute("id", "参数设置");
    fireEvent.click(screen.getByRole("link", { name: "跳到参数" }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("preserves viking URIs for the workspace link handler", () => {
    const onLinkClick = vi.fn(() => true);
    render(
      <MarkdownDocument
        content={"[来源](viking://resources/docs/guide.md#安装)"}
        onLinkClick={onLinkClick}
      />,
    );

    const link = screen.getByRole("link", { name: "来源" });
    expect(link).toHaveAttribute("href", "viking://resources/docs/guide.md#%E5%AE%89%E8%A3%85");
    fireEvent.click(link);
    expect(onLinkClick).toHaveBeenCalledWith("viking://resources/docs/guide.md#%E5%AE%89%E8%A3%85");
  });

  it("scrolls to a requested anchor after a different document is rendered", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<MarkdownDocument content={"# 证据详情"} anchor="证据详情" />);

    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1));
  });

  it("still rejects unsafe script URLs", () => {
    render(<MarkdownDocument content={"[危险](javascript:alert(1))"} />);
    expect(screen.getByText("危险").closest("a")).toHaveAttribute("href", "");
  });
});
