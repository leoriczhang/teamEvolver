// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { webcrypto } from "node:crypto";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import OntologyView from "./OntologyView";

vi.mock("@/lib/toast", () => ({ toastErr: vi.fn() }));
let enabled: boolean;
let writes: { path: string; init: RequestInit }[];
let failBuild: boolean;
let groups: any[];
let jobs: any[];
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const proposal = { revision: "wiki-v1", entity_types: ["Document"], predicates: {}, rules: [], issue_pack_revision: "wiki-v1" };

beforeEach(() => {
  enabled = false; writes = []; groups = []; jobs = []; failBuild = false;
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    if (init.method === "PUT" || init.method === "POST") {
      writes.push({ path, init });
      if (new Headers(init.headers).get("Content-Type") !== "application/json") {
        return reply({ detail: "INVALID_JSON" }, 422);
      }
      const body = init.body ? JSON.parse(String(init.body)) : {};
      if (path.endsWith("/access")) {
        enabled = true;
        return reply({ account: "product_agent", subject: "team", active: true, enabled: true });
      }
      if (path.endsWith("/schemas")) return reply({ revision: "wiki-v1" });
      if (path.endsWith("/source-collections")) {
        const group = { id: "ont_sources", state: "sources_ready", result: { stage: "sources_ready", refs: [{ source_id: "s", revision: "r", digest: "d" }], gaps: [] } };
        groups = [group]; return reply(group, 202);
      }
      if (path.endsWith("/jobs")) {
        if (failBuild) throw new TypeError("Failed to fetch");
        const job = { id: "ont_test", key: body.submission_key, state: "queued", result: {}, request: body };
        jobs = [job]; return reply(job, 202);
      }
      if (path.endsWith("/schema-confirm")) { jobs[0].state = "queued"; return reply(jobs[0]); }
    }
    if (path.endsWith("/capabilities")) return enabled ? reply({ generation: 0 }) : reply({ detail: "ONTOLOGY_DISABLED" }, 403);
    if (path.endsWith("/jobs")) return reply(jobs);
    if (path.endsWith("/source-collections")) return reply(groups);
    if (path.endsWith("/feedback")) return reply([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("enables a disabled account through the actual API client and shows the save receipt", async () => {
  render(<OntologyView active scope="test" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("ONTOLOGY_DISABLED");
  fireEvent.click(screen.getByRole("button", { name: "使用反馈" }));
  fireEvent.change(screen.getByRole("textbox", { name: "OV 用户标识" }), { target: { value: "team" } });
  fireEvent.change(screen.getByRole("combobox", { name: "权限模板" }), { target: { value: "operator" } });
  const save = screen.getByRole("button", { name: "保存 OV 授权" });
  await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
  expect(await screen.findByRole("status")).toHaveTextContent("账户 product_agent，用户 team");
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(JSON.parse(String(writes[0].init.body))).toEqual({ subject: "team", enabled: true, active: true, permissions: ["build", "approve", "publish", "read", "feedback"] });
});

async function freeze() {
  fireEvent.change(screen.getByRole("textbox", { name: "可读用户" }), { target: { value: "team" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Viking 来源 URI" }), { target: { value: "  viking://resources/wiki/  " } });
  fireEvent.click(screen.getByRole("button", { name: "创建并冻结来源集合" }));
  const start = screen.getByRole("button", { name: "使用这些来源构建" });
  await waitFor(() => expect(start).toBeEnabled()); fireEvent.click(start);
}

it("accepts a folder and submits on HTTP, reusing the request after a lost response", async () => {
  enabled = true;
  const random = vi.fn(webcrypto.getRandomValues.bind(webcrypto));
  vi.stubGlobal("crypto", { getRandomValues: random });
  render(<OntologyView active scope="http-test" />);
  await freeze();
  expect(JSON.parse(String(writes[0].init.body)).roots).toEqual(["viking://resources/wiki/"]);
  failBuild = true;
  const build = screen.getByRole("button", { name: "开始 Compile／受理对账" }); fireEvent.click(build);
  await screen.findByRole("alert");
  const first = writes.find(w => w.path.endsWith("/jobs"))!;
  const body = JSON.parse(String(first.init.body));
  expect(body).toMatchObject({ contract: "sf.te.ontology.compile.v1", collection_id: "ont_sources", expected_generation: 0 });
  expect(body.submission_key).toMatch(/^[0-9a-f-]{36}$/);
  failBuild = false; await waitFor(() => expect(build).toBeEnabled()); fireEvent.click(build);
  await screen.findByText(`${body.submission_key} · queued`);
  const attempts = writes.filter(w => w.path.endsWith("/jobs"));
  expect(attempts).toHaveLength(2); expect(attempts[1].init.body).toBe(first.init.body);
  expect(random).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("restores saved source collections after a page reload without refreezing", async () => {
  enabled = true;
  groups = [{ id: "saved", state: "sources_ready", result: { refs: [1], gaps: [{ code: "PERMISSION_DENIED" }] } }];
  render(<OntologyView active scope="test" />);
  await screen.findByRole("option", { name: /saved/ });
  fireEvent.change(screen.getByRole("combobox", { name: "来源集合" }), { target: { value: "saved" } });
  expect(screen.getByRole("button", { name: "使用这些来源构建" })).toBeEnabled();
  expect(screen.getByText(/缺口 1/)).toBeInTheDocument(); expect(writes).toHaveLength(0);
});

it("confirms a proposed Schema with its expected digest", async () => {
  enabled = true;
  jobs = [{ id: "ont_test", key: "wiki-build", state: "schema_review", result: { output_digest: "hash", output: { schema: proposal } } }];
  render(<OntologyView active scope="test" />);
  fireEvent.click(screen.getByRole("button", { name: "构建" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看" }));
  expect(screen.getByRole("textbox", { name: "Schema 定义" })).toHaveValue(JSON.stringify(proposal, null, 2));
  fireEvent.click(screen.getByRole("button", { name: "确认 Schema 并生成候选" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(JSON.parse(String(writes[0].init.body))).toEqual({ schema_body: proposal, expected_digest: "hash" });
});

it("sends the default Skill URI when the optional field is blank", async () => {
  enabled = true;
  render(<OntologyView active scope="default-skill" />);
  await freeze();
  expect(screen.getByRole("textbox", { name: "编译 Skill URI" })).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "开始 Compile／受理对账" }));
  await waitFor(() => expect(writes.filter(w => w.path.endsWith("/jobs"))).toHaveLength(1));
  const body = JSON.parse(String(writes.find(w => w.path.endsWith("/jobs"))!.init.body));
  expect(body.skill_uri).toBe("viking://agent/skills/ontology-extraction-v1");
});

it("submits a custom Skill URI and keeps it unchanged while reconciling a lost response", async () => {
  enabled = true;
  render(<OntologyView active scope="custom-skill" />);
  await freeze();
  const field = screen.getByRole("textbox", { name: "编译 Skill URI" });
  fireEvent.change(field, { target: { value: "  viking://agent/skills/ontology-extraction-copy  " } });
  failBuild = true;
  fireEvent.click(screen.getByRole("button", { name: "开始 Compile／受理对账" }));
  await screen.findByRole("alert");
  expect(field).toBeDisabled();
  const first = writes.find(w => w.path.endsWith("/jobs"))!;
  expect(JSON.parse(String(first.init.body)).skill_uri).toBe("viking://agent/skills/ontology-extraction-copy");
  // Reload the component; recovery does not require selecting or re-freezing the collection.
  cleanup(); failBuild = false;
  render(<OntologyView active scope="custom-skill" />);
  fireEvent.click(screen.getByRole("button", { name: "构建" }));
  expect(screen.getByRole("textbox", { name: "编译 Skill URI" })).toHaveValue("viking://agent/skills/ontology-extraction-copy");
  const retry = screen.getByRole("button", { name: "开始 Compile／受理对账" });
  await waitFor(() => expect(retry).toBeEnabled()); fireEvent.click(retry);
  await waitFor(() => expect(writes.filter(w => w.path.endsWith("/jobs"))).toHaveLength(2));
  expect(writes.filter(w => w.path.endsWith("/jobs"))[1].init.body).toBe(first.init.body);
});
