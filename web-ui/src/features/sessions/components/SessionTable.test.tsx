// @vitest-environment jsdom
import { cleanup,render,screen } from "@testing-library/react";
import { afterEach,expect,test } from "vitest";
import SessionTable from "./SessionTable";

afterEach(cleanup);

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
  expect(screen.getByText("audio · 版本未知")).toBeTruthy();
  expect(screen.getByText("delivery · 版本未知")).toBeTruthy();
  expect(screen.getByText("可用（未确认调用）")).toBeTruthy();
});

test("labels an explicit default as attribution instead of a verified submitter", () => {
  render(<SessionTable rows={[{
    session_id: "default-owner", user_alias: "operator", meta: { user_attribution: "connection_default" },
  }, {
    session_id: "unidentified-owner", user_alias: "未知提交人",
    meta: { user_id: "unknown.internal-id", user_attribution: "identity_unavailable" },
  }]} />);
  expect(screen.getByText("默认归属").getAttribute("title")).toBe("原会话未提供提交人，按连接配置归属");
  expect(screen.getByText("未知提交人")).toBeTruthy();
  expect(screen.queryByText("unknown.internal-id")).toBeNull();
});

test("shows historical versions beside the actually used Skill names", () => {
  const row = {
    session_id: "historical-use", used_skills: ["audio"], available_skills: ["audio", "delivery"],
    used_skill_versions: [{
      name: "audio", skill_id: "audio-id", version: "2", agent_id: "root",
      agent_name: "director", agent_role: "main", usage: "used",
    }],
  };
  render(<SessionTable rows={[row]} />);
  expect(screen.getByText("audio · v2")).toBeTruthy();
});

test("shows configured names and historical versions even with no recorded Skill call", () => {
  render(<SessionTable rows={[{
    session_id: "configured-only", used_skills: [], available_skills: ["audio", "delivery"],
    available_skill_versions: [
      { name: "audio", version: "2", agent_name: "director" },
      { name: "delivery", version: "4", agent_name: "writer" },
    ],
  }]} />);
  expect(screen.getByText("实际使用 0 / 可用 2")).toBeTruthy();
  expect(screen.getByText("audio · v2")).toBeTruthy();
  expect(screen.getByText("delivery · v4")).toBeTruthy();
});

test("keeps different historical versions and expands a large catalog", () => {
  render(<SessionTable rows={[{
    session_id: "catalog", available_skills: ["audio", "delivery", "captions", "layout"],
    available_skill_versions: [
      { name: "audio", version: "1", agent_name: "director" },
      { name: "audio", version: "2", agent_name: "writer" },
    ],
  }]} />);
  expect(screen.getByText("audio · v1")).toBeTruthy();
  expect(screen.getByText("audio · v2")).toBeTruthy();
  const remaining = screen.getByText("展开其余 2 项").closest("details")!;
  expect(remaining.open).toBe(false);
  expect(remaining.textContent).toContain("layout · 版本未知");
});
