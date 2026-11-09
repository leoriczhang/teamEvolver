// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ReplayExecutionEvidence from "./ReplayExecutionEvidence";

afterEach(cleanup);

describe("Replay execution evidence", () => {
  it("keeps old Skill-only observations unchanged", () => {
    const { container } = render(<ReplayExecutionEvidence branch={{ execution_verification: "not_required" }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the dispatched final Prompt, destination and request identity for each branch", () => {
    render(<ReplayExecutionEvidence branch={{ execution_verification: "verified", execution_plan_hash: "plan-hash",
      observed_requests: [{ dispatch_id: "dispatch-one", request_hash: "request-hash",
        observation: "remote_attested_dispatch", runtime_fingerprint: "agentscope-java-2.0.4",
        destination: { host: "model.example", path: "/chat/completions" },
        request: { messages: [{ role: "system", content: "Actually dispatched business Prompt" }] } }],
    }} />);
    fireEvent.click(screen.getByText("最终模型请求加载证据 · verified"));
    expect(screen.getByText("执行计划哈希：plan-hash")).toBeVisible();
    expect(screen.getByText("派发 ID：dispatch-one")).toBeVisible();
    expect(screen.getByText("证据来源：远端模型传输签名核验（remote_attested_dispatch）")).toBeVisible();
    expect(screen.getByText(/Runtime 指纹/)).toHaveTextContent("agentscope-java-2.0.4");
    expect(screen.getByText(/Actually dispatched business Prompt/)).toBeVisible();
    expect(screen.getByText(/model.example/)).toBeVisible();
    expect(screen.getByText(/自报不等同于模型实际使用/)).toBeVisible();
  });

  it("does not treat a reported verification string without dispatch records as loading proof", () => {
    render(<ReplayExecutionEvidence branch={{ execution_verification: "unsupported", execution_plan_hash: "reported-hash" }} />);
    fireEvent.click(screen.getByText("最终模型请求加载证据 · unsupported"));
    expect(screen.getByText("没有完整派发记录，不能仅凭状态或哈希确认实际加载。")).toBeVisible();
    expect(screen.queryByText(/派发 ID/)).not.toBeInTheDocument();
  });
});
