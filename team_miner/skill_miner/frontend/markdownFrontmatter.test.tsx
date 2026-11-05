// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { markdownBodyForPreview } from "./markdownFrontmatter";

afterEach(cleanup);

describe("artifact Markdown frontmatter", () => {
  it("renders the Chinese body heading without turning machine metadata into a heading", () => {
    const source = "---\nname: phone-appointment\ndescription: 中文说明\ncategory: workflow\n---\n\n# 电话约访\n\n中文步骤。";
    render(<MarkdownDocument content={markdownBodyForPreview(source)} />);
    expect(screen.getByRole("heading", { level: 1, name: "电话约访" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
    expect(screen.queryByText(/phone-appointment|category:|description:/)).not.toBeInTheDocument();
    expect(source).toContain("name: phone-appointment");
  });

  it("accepts arbitrary mapping keys, multiline YAML, BOM and CRLF without changing body text", () => {
    const body = "\r\n# 中文标题\r\n\r\n---\r\n\r\n正文";
    const source = "\uFEFF---\r\nlabel: 中文\r\nsummary: >\r\n  多行说明\r\n  第二行\r\ntags:\r\n  - 训练\r\n---\r\n" + body;
    expect(markdownBodyForPreview(source)).toBe(body);
  });

  it.each([
    "---\n\n普通正文\n\n---\n\n# 后续内容",
    "---\n# 正文标题\n\n普通正文\n---\n后续内容",
    "---\n标题\n---\n正文",
    "---\nlabel: [未闭合\n---\n# 正文",
    "---\nlabel: 中文\n# 没有结束标记",
    "---\n\n---\n正文",
    "# 正文\n\n---\nlabel: 这也是正文\n---",
  ])("preserves non-frontmatter and malformed source: %s", (source) => {
    expect(markdownBodyForPreview(source)).toBe(source);
  });
});
