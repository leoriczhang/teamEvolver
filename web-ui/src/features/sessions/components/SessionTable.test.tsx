// @vitest-environment jsdom
import { render,screen } from "@testing-library/react";
import { expect,test } from "vitest";
import SessionTable from "./SessionTable";

test("shows the conversation title as the primary label and keeps Session ID as metadata", () => {
  render(
    <SessionTable
      rows={[{
        session_id: "session_123456",
        title: "解释缓存命中后为什么不访问数据库",
        user_alias: "operator",
      }]}
    />,
  );

  expect(screen.getByText("解释缓存命中后为什么不访问数据库")).toBeTruthy();
  expect(screen.getByText("session_123456").className).toContain("mono");
});

test("distinguishes actual Skill use from the configured catalog", () => {
  render(<SessionTable rows={[{
    session_id: "with-catalog", used_skills: ["audio"], available_skills: ["audio", "delivery"],
  }]} />);
  expect(screen.getByText("实际使用 1 / 可用 2")).toBeTruthy();
  expect(screen.getByText("audio")).toBeTruthy();
  expect(screen.queryByText("delivery")).toBeNull();
});
