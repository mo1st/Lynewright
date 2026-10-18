/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: { openApiOperations: vi.fn() },
}));
vi.mock("./OpenApiResourceMappingsSection", () => ({ default: () => <div>OpenAPI resource mappings</div> }));
vi.mock("./OpenApiContractMappingsSection", () => ({ default: () => <div>OpenAPI contract mappings</div> }));

const { default: OpenApiManagementSection } = await import("./OpenApiManagementSection");

const summary = (runtime: string): IntegrationManagementSummary => ({
  integration: { id: "manual", name: "Manual", version: "1", type: "service", status: "disabled", enabled: false, requested_capabilities: [], granted_capabilities: [], permission_mode: "restricted", last_error: "", configuration: { manual: { runtime } } },
  availability: { enabled: false, configured: true, manual: true },
  capabilities: { requested: [], granted: [], permission_mode: "restricted" },
  connections: [],
  management: { sections: ["operations"], resource_types: [], supported_actions: [] },
  activity: { connection_count: 1, resource_count: 0, binding_count: 0 }, setup_requirements: [], health: {}, attention: [], attention_items: [], oauth: { supported: false }, inbound: [],
});

describe("OpenApiManagementSection runtime gate", () => {
  let host: HTMLDivElement;
  let root: Root;
  const mutate: Mutate = async (_key, action) => action();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(integrationManagementApi.openApiOperations).mockResolvedValue({ items: [] });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("does not render or fetch OpenAPI controls for an MCP runtime", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<OpenApiManagementSection integration={summary("mcp")} connectionId="primary" busy={false} mutate={mutate} />);
      await Promise.resolve();
    });
    expect(host.textContent).toBe("");
    expect(integrationManagementApi.openApiOperations).not.toHaveBeenCalled();
  });

  it("renders and fetches OpenAPI controls for an OpenAPI runtime", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<OpenApiManagementSection integration={summary("openapi")} connectionId="primary" busy={false} mutate={mutate} />);
      await Promise.resolve();
    });
    expect(host.textContent).toContain("OpenAPI resource mappings");
    expect(integrationManagementApi.openApiOperations).toHaveBeenCalledWith("manual", "primary");
  });
});
