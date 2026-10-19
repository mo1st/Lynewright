/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    openApiContractMappings: vi.fn(),
    mcpContractTargets: vi.fn(),
  },
}));

const { default: OpenApiContractMappingsSection } = await import("./OpenApiContractMappingsSection");

describe("OpenApiContractMappingsSection write policy", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(integrationManagementApi.openApiContractMappings).mockResolvedValue({ items: [] });
    vi.mocked(integrationManagementApi.mcpContractTargets).mockResolvedValue({
      items: [{
        contract: "lynewright.contacts", operation: "update", required_capability: "publishing:write",
        risk_class: "high", output_schema: { type: "object" },
      }],
    });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("exposes an explicit write opt-in and shows risk and destructive policy", async () => {
    const mutate: Mutate = async (_key, action) => action();
    await act(async () => {
      root = createRoot(host);
      root.render(<OpenApiContractMappingsSection
        integrationId="contacts"
        connectionId="primary"
        integrationEnabled={false}
        connectionSelected={true}
        busy={false}
        operations={[{
          operation_id: "updateContact", method: "DELETE", path: "/contacts/{id}",
          summary: "Delete a contact", approved: true, destructive: true,
        }]}
        mutate={mutate}
      />);
      await Promise.resolve();
    });

    const add = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add mapping");
    expect(add).toBeDefined();
    await act(async () => { add?.click(); });

    const selects = host.querySelectorAll("select");
    await act(async () => {
      const operation = selects[0] as HTMLSelectElement;
      operation.value = "updateContact";
      operation.dispatchEvent(new Event("change", { bubbles: true }));
      const target = selects[1] as HTMLSelectElement;
      target.value = "lynewright.contacts:update";
      target.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(host.textContent).toContain("Enable explicit WRITE opt-in for this mapping");
    expect(host.textContent).toContain("Contract risk: high");
    expect(host.textContent).toContain("remote operation: DELETE /contacts/{id}");
    expect(host.textContent).toContain("remote destructive: yes");
  });
});
