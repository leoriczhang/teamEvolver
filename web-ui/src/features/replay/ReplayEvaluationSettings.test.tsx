// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import ReplayEvaluationSettings from "./ReplayEvaluationSettings";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toastOk: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const external = {
  url: "https://judge.example.com/score", secret_ref: "secret-ref:judge", timeout_seconds: 60,
  rubric: "Evaluate business correctness", min_score: 70, min_improvement: 2,
};

async function open() {
  fireEvent.click(screen.getByText("A/B 回放评价方式"));
  return screen.findByLabelText("评价方式");
}

describe("Replay evaluation settings", () => {
  it("saves an external evaluator adapter reference with no mandatory HTTP URL", async () => {
    const adapter = { file: "score.py", revision: 4, source_hash: "d".repeat(64) };
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === "/api/replay-evaluator-adapter") return { available: [{ ...adapter, enabled: true }], source_editable: false };
      return options?.method === "PUT" ? { revision: 8, evaluation: JSON.parse(options.body as string).evaluation }
        : { revision: 7, evaluation: { mode: "checklist_efficiency" } };
    });
    render(<ReplayEvaluationSettings active isAdmin />);
    fireEvent.change(await open(), { target: { value: "external_agent" } });
    fireEvent.change(screen.getByLabelText("外部评测器接入方式"), { target: { value: "adapter" } });
    await waitFor(() => expect(screen.getByLabelText("外部评测器适配器文件")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("外部评测器适配器文件"), { target: { value: `${adapter.file}:${adapter.revision}:${adapter.source_hash}` } });
    expect(screen.getByRole("button", { name: "保存回放评价方式" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "保存回放评价方式" }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "PUT")).toBe(true));
    const request = vi.mocked(api).mock.calls.find(([, options]) => options?.method === "PUT");
    expect(JSON.parse(request![1]!.body as string)).toMatchObject({ expected_revision: 7,
      evaluation: { mode: "external_agent", external_agent: { url: "", adapter } } });
  });

  it("preserves existing evaluation by default and saves an explicit external configuration with CAS", async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => options?.method === "PUT"
      ? { revision: 8, evaluation: JSON.parse(options.body as string).evaluation }
      : { revision: 7, evaluation: { mode: "checklist_efficiency" } });
    render(<ReplayEvaluationSettings active isAdmin />);
    expect(api).not.toHaveBeenCalled();
    const mode = await open();
    expect(mode).toHaveValue("checklist_efficiency");
    fireEvent.change(mode, { target: { value: "external_agent" } });
    fireEvent.change(screen.getByLabelText("评价 Agent HTTP URL"), { target: { value: external.url } });
    fireEvent.change(screen.getByLabelText("认证 Secret Ref（可选）"), { target: { value: external.secret_ref } });
    fireEvent.change(screen.getByLabelText("评价要求（可选）"), { target: { value: external.rubric } });
    fireEvent.change(screen.getByLabelText("候选最低平均分"), { target: { value: "70" } });
    fireEvent.change(screen.getByLabelText("最低平均分提升"), { target: { value: "2" } });
    expect(vi.mocked(api).mock.calls.filter(([, o]) => o?.method === "PUT")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "保存回放评价方式" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/replay-evaluation", expect.objectContaining({ method: "PUT" })));
    const request = vi.mocked(api).mock.calls.find(([, o]) => o?.method === "PUT");
    expect(JSON.parse(request![1]!.body as string)).toEqual({
      expected_revision: 7, evaluation: { mode: "external_agent", external_agent: external },
    });
    expect(screen.queryByLabelText(/API Key/)).toBeNull();
    expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("validate") || path.includes("evolve"))).toBe(false);
  });

  it("keeps external settings when returning to the original mode and does not alter ongoing runs", async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => options?.method === "PUT"
      ? { revision: 5, evaluation: JSON.parse(options.body as string).evaluation }
      : { revision: 4, evaluation: { mode: "external_agent", external_agent: external } });
    render(<ReplayEvaluationSettings active isAdmin />);
    const mode = await open();
    fireEvent.change(mode, { target: { value: "checklist_efficiency" } });
    fireEvent.click(screen.getByRole("button", { name: "保存回放评价方式" }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([, o]) => o?.method === "PUT")).toBe(true));
    const request = vi.mocked(api).mock.calls.find(([, o]) => o?.method === "PUT");
    expect(JSON.parse(request![1]!.body as string).evaluation).toEqual({ mode: "checklist_efficiency", external_agent: external });
    fireEvent.change(mode, { target: { value: "external_agent" } });
    expect(screen.getByLabelText("评价 Agent HTTP URL")).toHaveValue(external.url);
  });

  it("renders evaluator settings read-only for non-admin users", async () => {
    vi.mocked(api).mockResolvedValue({ revision: 2, evaluation: { mode: "external_agent", external_agent: external } });
    render(<ReplayEvaluationSettings active isAdmin={false} />);
    expect(await open()).toBeDisabled();
    expect(screen.getByLabelText("评价 Agent HTTP URL")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存回放评价方式" })).toBeNull();
  });

  it("does not submit invalid scores and preserves the user's draft on revision conflict", async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => {
      if (options?.method === "PUT") throw new Error("配置已被修改，请重新加载");
      return { revision: 2, evaluation: { mode: "external_agent", external_agent: external } };
    });
    render(<ReplayEvaluationSettings active isAdmin />);
    await open();
    fireEvent.change(screen.getByLabelText("候选最低平均分"), { target: { value: "101" } });
    expect(screen.getByRole("button", { name: "保存回放评价方式" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("候选最低平均分"), { target: { value: "75" } });
    fireEvent.click(screen.getByRole("button", { name: "保存回放评价方式" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("配置已被修改，请重新加载");
    expect(screen.getByLabelText("候选最低平均分")).toHaveValue(75);
    expect(vi.mocked(api).mock.calls.filter(([, o]) => o?.method === "PUT")).toHaveLength(1);
  });
});
