/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    updateAuthorization: vi.fn(),
    manualCapabilities: vi.fn(),
  },
}));

const { default: AuthorizationSection } = await import("./AuthorizationSection");
const { default: HealthDiagnosticsSection } = await import("./HealthDiagnosticsSection");

describe("integration state display", () => {
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

  it("shows full access as the effective host policy", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<AuthorizationSection integrationId="manual" requested={["network:use"]} permissionMode="full" granted={[]} busy={false} mutate={mutate} />);
      await Promise.resolve();
    });
    expect(host.textContent).toContain("Full access (host policy)");
    expect(host.textContent).not.toContain("Granted: none");
  });

  it("keeps manual capability declaration separate from the grant", async () => {
    vi.mocked(integrationManagementApi.manualCapabilities).mockResolvedValue({ items: ["network:use", "publishing:write"] });
    vi.mocked(integrationManagementApi.updateAuthorization).mockResolvedValue({} as never);
    await act(async () => {
      root = createRoot(host);
      root.render(<AuthorizationSection integrationId="manual" requested={["network:use"]} permissionMode="custom" granted={["network:use"]} manual integrationEnabled={false} busy={false} mutate={mutate} />);
      await Promise.resolve();
    });

    const declaration = host.querySelector<HTMLInputElement>("#integration-declaration-publishing-write")!;
    await act(async () => { declaration.click(); });
    expect(host.textContent).toContain("Declaring a capability does not grant it");
    const grant = host.querySelector<HTMLInputElement>("#integration-capability-publishing-write")!;
    expect(grant.checked).toBe(false);
    await act(async () => { grant.click(); });
    await act(async () => { Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Save authorization")?.click(); await Promise.resolve(); });
    expect(integrationManagementApi.updateAuthorization).toHaveBeenCalledWith("manual", {
      permission_mode: "custom",
      requested_capabilities: ["network:use", "publishing:write"],
      granted_capabilities: ["network:use", "publishing:write"],
    });
  });

  it("treats the manual runtime state as normal and shows the latest test time", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<HealthDiagnosticsSection integrationId="manual" fallback={{ status: "NOT_EXECUTABLE" }} enabled={false} manual lastChecked="2026-09-02T20:00:00Z" />);
      await Promise.resolve();
    });
    expect(host.textContent).toContain("NOT_EXECUTABLE · normal");
    expect(host.textContent).toContain("Manual integrations execute through the selected connection");
    expect(host.textContent).toContain("Last tested: 2026-09-02T20:00:00Z");
    expect(host.textContent).not.toContain("Review the connection");
    expect(integrationManagementApi.updateAuthorization).not.toHaveBeenCalled();
  });
});
