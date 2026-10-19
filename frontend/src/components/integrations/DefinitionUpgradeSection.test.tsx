/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    definitionUpgradePreview: vi.fn(),
    definitionUpgradeApply: vi.fn(),
  },
}));

const { default: DefinitionUpgradeSection } = await import("./DefinitionUpgradeSection");

const RECONCILE_PREVIEW = {
  mode: "deployed_reconcile",
  current: { definition_version: "0.5.0", manifest_fingerprint: "7b31261e6cf0b03d1f366e879375d0a3" },
  candidate: { definition_version: "0.5.1", manifest_fingerprint: "1647c54ef9e92b1a8099fda0d5b994b2" },
  diff: {
    definition_version: { from: "0.5.0", to: "0.5.1" },
    manifest_fingerprint: { from: "7b31261e6cf0b03d1f366e879375d0a3", to: "1647c54ef9e92b1a8099fda0d5b994b2" },
  },
  rollback: "redeploy_previous_release",
  capability_change: { known: true, changed: true, added: { "lynewright.publishing.factory": ["run.status"] }, removed: {} },
};

describe("DefinitionUpgradeSection", () => {
  let host: HTMLDivElement;
  let root: Root;
  const mutate: Mutate = async (_key, action) => action();

  const render = async (props: Partial<{ runtimeUnavailable: boolean; busy: boolean }> = {}) => {
    await act(async () => {
      root = createRoot(host);
      root.render(<DefinitionUpgradeSection
        integrationId="lynewright.integration.processing-pipeline"
        runtimeUnavailable={props.runtimeUnavailable ?? false}
        busy={props.busy ?? false}
        mutate={mutate}
      />);
    });
  };

  const click = async (label: string) => {
    const button = [...host.querySelectorAll("button")].find((item) => item.textContent?.includes(label));
    expect(button, `no button matching ${label}`).toBeTruthy();
    await act(async () => { button!.click(); });
  };

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

  it("writes nothing until apply is pressed", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    await render();
    await click("Preview deployed definition");

    expect(integrationManagementApi.definitionUpgradePreview).toHaveBeenCalledOnce();
    expect(integrationManagementApi.definitionUpgradeApply).not.toHaveBeenCalled();
  });

  it("shows what the change moves, old and new", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    await render();
    await click("Preview deployed definition");

    const text = host.textContent ?? "";
    expect(text).toContain("0.5.0");
    expect(text).toContain("0.5.1");
    expect(text).toContain("Definition version");
  });

  it("says plainly that a reconcile cannot be undone from the dashboard", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    await render();
    await click("Preview deployed definition");

    expect(host.textContent).toContain("deploying the previous release again");
  });

  it("keeps apply disabled until the change is acknowledged", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    await render();
    await click("Preview deployed definition");

    const apply = [...host.querySelectorAll("button")].find((item) => item.textContent?.includes("Apply deployed definition"));
    expect(apply?.disabled).toBe(true);

    const confirm = host.querySelector<HTMLInputElement>("#definition-upgrade-confirm");
    await act(async () => { confirm!.click(); });
    expect(apply?.disabled).toBe(false);
  });

  it("sends the reviewed identity so the server can refuse a moved target", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    vi.mocked(integrationManagementApi.definitionUpgradeApply).mockResolvedValue({ mode: "deployed_reconcile" });
    await render();
    await click("Preview deployed definition");

    const confirm = host.querySelector<HTMLInputElement>("#definition-upgrade-confirm");
    await act(async () => { confirm!.click(); });
    await click("Apply deployed definition");

    expect(integrationManagementApi.definitionUpgradeApply).toHaveBeenCalledWith(
      "lynewright.integration.processing-pipeline",
      {
        expected_definition_version: "0.5.1",
        expected_manifest_fingerprint: "1647c54ef9e92b1a8099fda0d5b994b2",
      },
    );
  });

  it("reports a settled lock instead of offering a no-op apply", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue({
      mode: "reversible_upgrade",
      current: { definition_version: "0.5.1" },
      candidate: { definition_version: "0.5.1" },
      diff: {},
    });
    await render();
    await click("Preview deployed definition");

    expect(host.textContent).toContain("already names the deployed definition");
    expect([...host.querySelectorAll("button")].some((item) => item.textContent?.includes("Apply deployed definition"))).toBe(false);
  });

  it("surfaces a failed preview without claiming anything changed", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockRejectedValue(
      new Error("This tenant action requires the 'admin' role or higher."),
    );
    await render();
    await click("Preview deployed definition");

    const alert = host.querySelector("[role='alert']");
    expect(alert?.textContent).toContain("admin");
    expect(integrationManagementApi.definitionUpgradeApply).not.toHaveBeenCalled();
  });

  it("explains the error state a stale lock produces", async () => {
    vi.mocked(integrationManagementApi.definitionUpgradePreview).mockResolvedValue(RECONCILE_PREVIEW);
    await render({ runtimeUnavailable: true });
    expect(host.textContent).toContain("Upgrade available");
    expect(host.textContent).toContain("changes declared capabilities");
  });
});
