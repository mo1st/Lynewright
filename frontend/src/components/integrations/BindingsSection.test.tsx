/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    bindings: vi.fn(),
    repairBinding: vi.fn(),
  },
}));

const { default: BindingsSection } = await import("./BindingsSection");

describe("BindingsSection", () => {
  let host: HTMLDivElement;
  let root: Root;
  const mutate: Mutate = async (_key, action) => action();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(integrationManagementApi.bindings).mockResolvedValue({
      items: [{ id: "binding-1", resource_type: "channel", project_id: "demo-project", state: "PENDING" }],
    });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("explains that a pending binding awaits lifecycle verification", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<BindingsSection integrationId="buffer" refreshToken={{} as IntegrationManagementSummary} busy={false} mutate={mutate} />);
      await Promise.resolve();
    });

    expect(host.textContent).toContain("Pending — awaiting lifecycle verification");
    expect(host.textContent).toContain("Awaiting verification");
    expect(host.textContent).not.toContain("Repair");
  });
});
