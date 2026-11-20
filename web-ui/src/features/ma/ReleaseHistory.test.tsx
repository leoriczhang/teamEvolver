// @vitest-environment jsdom
import { expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import ReleaseHistory from "./ReleaseHistory";

test("发布记录显示固定环境别名和真实名称", () => {
  render(<ReleaseHistory releases={[{
    release_id: "ma-release:test", status: "applied", created_at: "2026-11-05T10:00:00Z",
    record: {
      environment_id: "env-new", environment_alias: "codingvideo-dev-runtime",
      environment_name: "codingvideo-dev-runtime-aba8d2f6bc0d",
    },
  }]} />);
  expect(screen.getByText(/codingvideo-dev-runtime → env-new/)).toBeTruthy();
  expect(screen.getByText(/实际名称 codingvideo-dev-runtime-aba8d2f6bc0d/)).toBeTruthy();
});
