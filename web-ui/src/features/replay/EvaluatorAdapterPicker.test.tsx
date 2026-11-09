// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import EvaluatorAdapterPicker from "./EvaluatorAdapterPicker";

vi.mock("@/api/client", () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const ref = { file: "business.py", revision: 3, source_hash: "a".repeat(64) };
const metadata = { ...ref, label: "Business scorer", enabled: true };

describe("EvaluatorAdapterPicker", () => {
  it("selects an immutable file version while preserving an already selected older version", async () => {
    vi.mocked(api).mockResolvedValue({ available: [metadata], source_editable: false });
    const onChange = vi.fn();
    render(<EvaluatorAdapterPicker value={{ ...ref, revision: 2, source_hash: "b".repeat(64) }} disabled={false} onChange={onChange} />);
    expect(await screen.findByText("business.py · v2（已固定的历史版本）")).toBeVisible();
    await waitFor(() => expect(screen.getByLabelText("外部评测器适配器文件")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("外部评测器适配器文件"), { target: { value: `${ref.file}:${ref.revision}:${ref.source_hash}` } });
    expect(onChange).toHaveBeenCalledWith(ref);
    expect(screen.queryByLabelText("上传评测器 Python 适配器")).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
  });

  it("saves edited source with CAS and selects the new version without changing evaluation settings", async () => {
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path.includes("/code?") && !options) return { ...ref, code: "original_code" };
      if (options?.method === "PUT") return { ...metadata, revision: 4, source_hash: "c".repeat(64) };
      return { available: [metadata], source_editable: true };
    });
    const onChange = vi.fn();
    render(<EvaluatorAdapterPicker value={ref} disabled={false} onChange={onChange} />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑所选评测器适配器" }));
    fireEvent.change(await screen.findByLabelText("评测器适配器 Python 源码"), { target: { value: "updated_code" } });
    fireEvent.click(screen.getByRole("button", { name: "保存评测器适配器版本" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...ref, revision: 4, source_hash: "c".repeat(64) }));
    const writes = vi.mocked(api).mock.calls.filter(([, options]) => options?.method === "PUT");
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe("/api/replay-evaluator-adapter/code");
    expect(JSON.parse(writes[0][1]?.body as string)).toEqual({ file: "business.py", code: "updated_code", expected_revision: 3 });
  });
});
