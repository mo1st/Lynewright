/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IntegrationInstallSummary } from "../../types";
import InstallConfirmationDialog from "./InstallConfirmationDialog";

describe("InstallConfirmationDialog", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.appendChild(host);
  });
  afterEach(() => { act(() => root?.unmount()); host.remove(); vi.clearAllMocks(); });

  it("keeps confirmation disabled until the exact id is typed and emphasizes in-process execution", async () => {
    const onConfirm = vi.fn();
    const summary = {
      integration_id: "lynewright.integration.fixture", name: "Fixture", version: "1.0.0",
      kind: "native package", source: "package:fixture==1.0.0", sha256: null,
      runs_code: true, execution: "runs INSIDE Lynewright with access to all data",
      requested_capabilities: ["network:use"], egress: "internet", credential_refs: [],
      media_access: "None", catalogue_status: "not listed",
    } as IntegrationInstallSummary;
    await act(async () => { root = createRoot(host); root.render(<InstallConfirmationDialog summary={summary} summaryHash={"a".repeat(64)} onConfirm={onConfirm} onCancel={vi.fn()} />); });
    const button = [...host.querySelectorAll("button")].find((item) => item.textContent === "Confirm install")!;
    expect(button.disabled).toBe(true);
    expect(host.querySelector(".install-confirmation--strong")).not.toBeNull();
    expect(host.textContent).toContain("Runs inside Lynewright with access to all data");
    const input = host.querySelector<HTMLInputElement>("#install-confirmation-id")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "lynewright.integration.fixture");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(button.disabled).toBe(false);
    await act(async () => { button.click(); });
    expect(onConfirm).toHaveBeenCalledWith({ integration_id: summary.integration_id, summary_hash: "a".repeat(64) });
  });
});
