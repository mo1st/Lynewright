/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    mcpResourceMappings: vi.fn(),
    previewMcpResourceMapping: vi.fn(),
    createMcpResourceMapping: vi.fn(),
    updateMcpResourceMapping: vi.fn(),
    deleteMcpResourceMapping: vi.fn(),
  },
}));

const { default: McpResourceMappingsSection } = await import("./McpResourceMappingsSection");

describe("McpResourceMappingsSection", () => {
  let host: HTMLDivElement;
  let root: Root;
  const tools = [
    { name: "listChannels", description: "List channels", approved: true },
    { name: "secretTool", description: "Not approved", approved: false },
  ];
  const mutate: Mutate = async (_key, action) => action();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(integrationManagementApi.mcpResourceMappings).mockResolvedValue({ items: [] });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("offers only approved tools and exposes bounded mapping paths", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<McpResourceMappingsSection integrationId="buffer" connectionId="primary" integrationEnabled={false} discoveredTools={tools} busy={false} mutate={mutate} reportError={vi.fn()} />);
      await Promise.resolve();
    });
    const add = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping");
    await act(async () => { add?.click(); });
    const listOptions = Array.from(host.querySelectorAll("#mcp-resource-list-tool option")).map((option) => (option as HTMLOptionElement).value);
    expect(listOptions).toEqual(["", "listChannels"]);
    expect(host.textContent).toContain("List arguments (bounded scalar JSON)");
    expect(host.textContent).toContain("Optional actions and response paths");
    expect(host.querySelector("#mcp-resource-items-path")).toBeTruthy();
    expect(host.querySelector("#mcp-resource-id-path")).toBeTruthy();
    expect(host.querySelector("#mcp-resource-name-path")).toBeTruthy();
  });

  it("distinguishes a failed resource list read and offers retry", async () => {
    vi.mocked(integrationManagementApi.mcpResourceMappings).mockRejectedValueOnce(new Error("temporary"));
    await act(async () => {
      root = createRoot(host);
      root.render(<McpResourceMappingsSection integrationId="buffer" connectionId="primary" integrationEnabled={false} discoveredTools={tools} busy={false} mutate={mutate} reportError={vi.fn()} />);
      await Promise.resolve();
    });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Retry");
    vi.mocked(integrationManagementApi.mcpResourceMappings).mockResolvedValueOnce({ items: [] });
    const retry = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Retry");
    await act(async () => { retry?.click(); await Promise.resolve(); });
    expect(host.textContent).toContain("No MCP resource mappings yet.");
  });
});
