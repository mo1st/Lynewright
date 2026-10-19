/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import { ConfirmProvider } from "../../context/ConfirmContext";
import type { IntegrationMcpTool } from "../../types";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    mcpContractMappings: vi.fn(),
    mcpContractTargets: vi.fn(),
    approveMcpTools: vi.fn(),
    createMcpContractMapping: vi.fn(),
    executeMcpContractMapping: vi.fn(),
  },
}));

const { default: McpContractMappingsSection } = await import("./McpContractMappingsSection");

describe("McpContractMappingsSection approval state", () => {
  let host: HTMLDivElement;
  let root: Root;
  let currentTools: IntegrationMcpTool[];

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    currentTools = [{ name: "publishTool", description: "Publish", approved: false }];
    vi.mocked(integrationManagementApi.mcpContractMappings).mockResolvedValue({ items: [] });
    vi.mocked(integrationManagementApi.mcpContractTargets).mockResolvedValue({ items: [] });
    vi.mocked(integrationManagementApi.approveMcpTools).mockResolvedValue({});
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("renders the server-refetched approval and uses it in the mapping selector", async () => {
    const render = () => root.render(<McpContractMappingsSection integrationId="buffer" connectionId="primary" discoveredTools={currentTools} enabled={false} connectionEnabled={true} busy={false} mutate={mutate} reportError={vi.fn()} />);
    const mutate: Mutate = async (_key, action) => {
      const result = await action();
      currentTools = currentTools.map((tool) => ({ ...tool, approved: true }));
      render();
      return result;
    };
    await act(async () => {
      root = createRoot(host);
      render();
      await Promise.resolve();
    });
    const approve = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Approve");
    await act(async () => { approve?.click(); await Promise.resolve(); });
    expect(host.textContent).toContain("Approved");
    const add = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping");
    await act(async () => { add?.click(); });
    const values = Array.from(host.querySelectorAll("#mcp-mapping-tool option")).map((option) => (option as HTMLOptionElement).value);
    expect(values).toEqual(["", "publishTool"]);
  });

  it("makes schema-backed social publish targets selectable", async () => {
    vi.mocked(integrationManagementApi.mcpContractTargets).mockResolvedValue({
      items: [
        { contract: "lynewright.social.publisher", operation: "posts.create", required_capability: "publishing:write", risk_class: "SAFE_OPERATION", output_schema: { type: "object" } },
        { contract: "lynewright.social.publisher", operation: "posts.schedule", required_capability: "publishing:write", risk_class: "SAFE_OPERATION", output_schema: { type: "object" } },
        { contract: "lynewright.social.publisher", operation: "posts.cancel", required_capability: "publishing:write", risk_class: "DESTRUCTIVE", output_schema: { type: "object" } },
      ],
    });
    await act(async () => {
      root = createRoot(host);
      root.render(<McpContractMappingsSection integrationId="buffer" connectionId="primary" discoveredTools={currentTools} enabled={false} connectionEnabled={true} busy={false} mutate={vi.fn()} reportError={vi.fn()} />);
      await Promise.resolve();
    });

    const add = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping");
    await act(async () => { add?.click(); });
    const values = Array.from(host.querySelectorAll("#mcp-mapping-target option")).map((option) => (option as HTMLOptionElement).value);
    expect(values).toEqual([
      ":",
      "lynewright.social.publisher:posts.create",
      "lynewright.social.publisher:posts.schedule",
      "lynewright.social.publisher:posts.cancel",
    ]);
  });

  it("shows a retryable load failure instead of a false empty state", async () => {
    vi.mocked(integrationManagementApi.mcpContractMappings).mockRejectedValue(new Error("offline"));
    await act(async () => {
      root = createRoot(host);
      root.render(<McpContractMappingsSection integrationId="buffer" connectionId="primary" discoveredTools={[]} enabled={false} connectionEnabled={true} busy={false} mutate={vi.fn()} reportError={vi.fn()} />);
      await Promise.resolve();
    });

    expect(host.textContent).toContain("could not be loaded");
    expect(host.textContent).not.toContain("No contract mappings");
    expect(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping")?.disabled).toBe(true);
    expect(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Retry")).toBeTruthy();
  });

  it("requires deliberate approval for a write-capable tool", async () => {
    currentTools = [{
      name: "create_post", description: "Create a post", approved: false,
      read_only: false, destructive: false, safety_known: true,
    }];
    const mutate: Mutate = async (_key, action) => action();
    await act(async () => {
      root = createRoot(host);
      root.render(<ConfirmProvider><McpContractMappingsSection integrationId="buffer" connectionId="primary" discoveredTools={currentTools} enabled={true} connectionEnabled={true} busy={false} mutate={mutate} reportError={vi.fn()} /></ConfirmProvider>);
      await Promise.resolve();
    });

    const approve = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Approve WRITE");
    await act(async () => { approve?.click(); await Promise.resolve(); });
    expect(host.querySelector('[role="alertdialog"]')?.textContent).toContain("It may change remote data");
    await act(async () => {
      Array.from(host.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')).find((button) => button.textContent === "Cancel")?.click();
      await Promise.resolve();
    });
    expect(integrationManagementApi.approveMcpTools).not.toHaveBeenCalled();

    await act(async () => { approve?.click(); await Promise.resolve(); });
    await act(async () => {
      Array.from(host.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')).find((button) => button.textContent === "Approve tool")?.click();
      await Promise.resolve();
    });
    expect(integrationManagementApi.approveMcpTools).toHaveBeenCalledWith("buffer", "primary", ["create_post"]);
  });

  it("shows risk and destructive policy and sends explicit write opt-in", async () => {
    currentTools = [{
      name: "delete_post", description: "Delete a post", approved: true,
      read_only: false, destructive: true, safety_known: true,
    }];
    vi.mocked(integrationManagementApi.mcpContractTargets).mockResolvedValue({ items: [{
      contract: "lynewright.social.publisher", operation: "posts.cancel", required_capability: "publishing:write",
      risk_class: "DESTRUCTIVE", output_schema: { type: "object" },
    }] });
    vi.mocked(integrationManagementApi.createMcpContractMapping).mockResolvedValue({
      mapping_id: "delete-map", tool_name: "delete_post", target_contract: "lynewright.social.publisher",
      target_operation: "posts.cancel", argument_mapping: {}, output_mapping: {}, enabled: true,
      write_enabled: true, destructive: true, risk_class: "DESTRUCTIVE",
    });
    const mutate: Mutate = async (_key, action) => action();
    await act(async () => {
      root = createRoot(host);
      root.render(<McpContractMappingsSection integrationId="buffer" connectionId="primary" discoveredTools={currentTools} enabled={false} connectionEnabled={true} busy={false} mutate={mutate} reportError={vi.fn()} />);
      await Promise.resolve();
    });

    await act(async () => { Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping")?.click(); });
    const tool = host.querySelector<HTMLSelectElement>("#mcp-mapping-tool")!;
    const target = host.querySelector<HTMLSelectElement>("#mcp-mapping-target")!;
    await act(async () => {
      tool.value = "delete_post";
      tool.dispatchEvent(new Event("change", { bubbles: true }));
      target.value = "lynewright.social.publisher:posts.cancel";
      target.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(host.textContent).toContain("Contract risk: DESTRUCTIVE");
    expect(host.textContent).toContain("tool policy: WRITE · destructive");
    await act(async () => { host.querySelector<HTMLInputElement>("#mcp-mapping-write")?.click(); });
    await act(async () => {
      host.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await Promise.resolve();
    });
    expect(integrationManagementApi.createMcpContractMapping).toHaveBeenCalledWith("buffer", "primary", expect.objectContaining({
      tool_name: "delete_post", target_operation: "posts.cancel", write_enabled: true,
    }));
  });
});
