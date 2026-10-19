/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: { probeMcpTool: vi.fn() },
}));

const { default: McpToolProbe } = await import("./McpToolProbe");

describe("McpToolProbe", () => {
  let host: HTMLDivElement;
  let root: Root;
  const mutate: Mutate = async (_key, action) => action();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("only offers the probe for an approved, explicitly read-only tool", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<McpToolProbe integrationId="fixture" connectionId="primary" tool={{ name: "list", description: "List", approved: true, read_only: true, destructive: false, safety_known: true }} busy={false} mutate={mutate} reportError={vi.fn()} />);
      await Promise.resolve();
    });
    expect(host.textContent).toContain("Probe read-only tool");
    expect(host.textContent).not.toContain("unavailable");
  });

  it("shows the bounded result and scalar copy affordance", async () => {
    vi.mocked(integrationManagementApi.probeMcpTool).mockResolvedValue({ integration_id: "fixture", connection_id: "primary", tool_name: "list", result: { organizationId: "org-1", count: 2 }, truncated: false, serialized_bytes: 42 });
    await act(async () => {
      root = createRoot(host);
      root.render(<McpToolProbe integrationId="fixture" connectionId="primary" tool={{ name: "list", description: "List", approved: true, read_only: true, destructive: false, safety_known: true }} busy={false} mutate={mutate} reportError={vi.fn()} />);
      await Promise.resolve();
    });
    const probe = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Probe list");
    await act(async () => { probe?.click(); await Promise.resolve(); });
    expect(host.textContent).toContain("organizationId");
    expect(host.textContent).toContain("Copy scalar values");
    expect(host.textContent).toContain("Copy");
  });

  it("does not render a probe for an unapproved or unsafe tool", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<><McpToolProbe integrationId="fixture" connectionId="primary" tool={{ name: "unapproved", description: "No", approved: false }} busy={false} mutate={mutate} reportError={vi.fn()} /><McpToolProbe integrationId="fixture" connectionId="primary" tool={{ name: "write", description: "Write", approved: true, read_only: false, destructive: true, safety_known: true }} busy={false} mutate={mutate} reportError={vi.fn()} /></>);
      await Promise.resolve();
    });
    expect(host.textContent).not.toContain("Probe unapproved");
    expect(host.textContent).toContain("Probe unavailable");
  });
});
