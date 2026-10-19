/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    importFile: vi.fn(),
    exportFile: vi.fn(),
  },
}));

const { default: IntegrationFileSection } = await import("./IntegrationFileSection");

describe("IntegrationFileSection", () => {
  let host: HTMLDivElement;
  let root: Root;

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

  it("shows every validation error returned by import", async () => {
    vi.mocked(integrationManagementApi.importFile).mockRejectedValue(new ApiError(422, "Unprocessable Entity", {
      errors: ["id: is required", "operations.0: invalid declaration"],
    }));
    await act(async () => {
      root = createRoot(host);
      root.render(<IntegrationFileSection selected={null} onImported={vi.fn()} />);
    });
    const importButton = [...host.querySelectorAll("button")].find((button) => button.textContent === "Import file");
    await act(async () => { importButton!.click(); });
    const input = host.querySelector<HTMLInputElement>("#integration-file-upload");
    const file = new File(["format: lynewright-integration/1"], "invalid.yaml", { type: "application/yaml" });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    await act(async () => { input!.dispatchEvent(new Event("change", { bubbles: true })); });
    const submit = [...host.querySelectorAll("button")].find((button) => button.textContent === "Import selected file");
    await act(async () => { submit!.click(); await Promise.resolve(); });

    expect(host.querySelector("[role='alert']")?.textContent).toContain("id: is required");
    expect(host.querySelector("[role='alert']")?.textContent).toContain("operations.0: invalid declaration");
    expect(integrationManagementApi.importFile).toHaveBeenCalledWith(file);
  });

  it("keeps the credential-reference guidance visible for secret-shaped fields", async () => {
    const message = "operations.0.input.password: literal values under secret-like keys are forbidden; declare a named credential ref under credentials.";
    vi.mocked(integrationManagementApi.importFile).mockRejectedValue(new ApiError(422, "Unprocessable Entity", { errors: [message] }));
    await act(async () => {
      root = createRoot(host);
      root.render(<IntegrationFileSection selected={null} embedded onImported={vi.fn()} />);
    });
    const paste = host.querySelector<HTMLTextAreaElement>("#integration-file-paste")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(paste, "format: lynewright-integration/1\nid: lynewright.integration.fixture\n");
      paste.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...host.querySelectorAll("button")].find((button) => button.textContent === "Import selected file")!.click(); await Promise.resolve(); });
    expect(host.querySelector("[role='alert']")?.textContent).toContain("declare a named credential ref under credentials");
  });

  it("offers export for a declarative integration only", async () => {
    const selected = {
      integration: { id: "lynewright.integration.example" },
      availability: { declarative: true },
    } as IntegrationManagementSummary;
    await act(async () => {
      root = createRoot(host);
      root.render(<IntegrationFileSection selected={selected} onImported={vi.fn()} />);
    });
    expect([...host.querySelectorAll("button")].some((button) => button.textContent === "Export")).toBe(true);
  });

  it("shows the import response when no operations were approved", async () => {
    vi.mocked(integrationManagementApi.importFile).mockResolvedValue({
      integration: {} as IntegrationManagementSummary, created: true, idempotent: false,
      approval_message: "0 operations approved; approve them in Integrations.",
    });
    await act(async () => {
      root = createRoot(host);
      root.render(<IntegrationFileSection selected={null} onImported={vi.fn()} />);
    });
    const open = [...host.querySelectorAll("button")].find((button) => button.textContent === "Import file");
    await act(async () => { open!.click(); });
    const input = host.querySelector<HTMLInputElement>("#integration-file-upload");
    const file = new File(["format: lynewright-integration/1"], "unapproved.yaml", { type: "application/yaml" });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    await act(async () => { input!.dispatchEvent(new Event("change", { bubbles: true })); });
    const submit = [...host.querySelectorAll("button")].find((button) => button.textContent === "Import selected file");
    await act(async () => { submit!.click(); await Promise.resolve(); });
    expect(host.querySelector("[role='status']")?.textContent)
      .toBe("0 operations approved; approve them in Integrations.");
  });
});
