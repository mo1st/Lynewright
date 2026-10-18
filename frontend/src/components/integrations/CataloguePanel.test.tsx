/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { catalogueApi, type CatalogueResponse } from "../../api/catalogueClient";
import { integrationManagementApi } from "../../api/integrationManagementClient";

vi.mock("../../api/catalogueClient", () => ({ catalogueApi: { get: vi.fn(), refresh: vi.fn(), install: vi.fn(), repair: vi.fn() } }));
vi.mock("../../api/integrationManagementClient", () => ({ integrationManagementApi: { list: vi.fn() } }));
const { default: CataloguePanel } = await import("./CataloguePanel");

function response(): CatalogueResponse {
  const common = { version: "1.0.0", licence: "MIT", lynewright_version: ">=0.1.0", permissions: { capabilities: ["render"], egress: "none" as const, media: "write" as const, credential_refs: [] as string[] }, review: { reviewer: "Reviewer", date: "2026-10-15", catalogue_commit: "1234567" } };
  return {
    status: { url: "https://catalogue.example.test/index.json", status: "ready", trusted_key_ids: [{ id: "dev:abc123", source: "built-in development key" }], dev_key_trusted: true, dev_key_warning: true, dev_key_warning_message: "Development catalogue key trusted, not for production.", verified_key_id: "dev:abc123", serial: 8, generated_at: "2026-10-15T12:00:00Z", fetched_at: "2026-10-15T12:01:00Z", last_error: null },
    entries: [
      { ...common, id: "fixture.template-openapi", name: "OpenAPI Example", summary: "Health check and asynchronous job API.", description: "First line.\nSecond line.", kind: "file", source: "https://example.test/file.yaml", sha256: "a".repeat(64), requires_operator_setup: ["Start the example and map LYNEWRIGHT_INTERNAL_ENDPOINT__<REF>."] },
      { ...common, id: "fixture.demo-package", name: "Native Python Example", summary: "A trusted-code starter.", description: "A trusted Python package.", kind: "package", source: "https://example.test/pkg.whl", sha256: "c".repeat(64) },
      { ...common, id: "fixture.no-name", kind: "container", source: "https://example.test/image@sha256:abc", sha256: "sha256:abc" },
      { ...common, id: "fixture.installed", name: "Installed example", kind: "package", source: "https://example.test/old.whl", sha256: "b".repeat(64), installed: true, provenance_kind: "catalogue", provenance_status: "catalogue_drift" },
      { ...common, id: "fixture.incomplete", name: "Incomplete package", kind: "package", source: "https://example.test/incomplete.whl", sha256: "e".repeat(64), installed: true, installation_incomplete: true, provenance_kind: "catalogue" },
      { ...common, id: "fixture.old", kind: "file", source: "https://example.test/old.yaml", sha256: "d".repeat(64), yanked: { reason: "withdrawn", date: "2026-10-15" } },
    ],
  };
}

const managed = [{ integration: { id: "fixture.installed", name: "Installed example", version: "1.0.0", type: "package", status: "enabled", enabled: true, requested_capabilities: [], granted_capabilities: [], permission_mode: "approved", last_error: "" }, provenance: { kind: "catalogue" as const, status: "catalogue_drift", recorded_at: "2026-10-15T12:00:00Z", signing_key_id: "dev:abc123", drift_reason: "Entry was yanked." }, availability: { enabled: true, configured: true, manual: false }, capabilities: { requested: [], granted: [], permission_mode: "approved" }, connections: [], management: { sections: [] } }];

describe("CataloguePanel", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
    vi.mocked(catalogueApi.get).mockResolvedValue(response());
    vi.mocked(integrationManagementApi.list).mockResolvedValue({ items: managed as never, package_statuses: [] });
  });
  afterEach(() => { act(() => root.unmount()); document.body.innerHTML = ""; vi.clearAllMocks(); });
  async function render() { await act(async () => { root.render(<CataloguePanel />); await Promise.resolve(); }); }
  async function click(button: Element | null) { await act(async () => { button?.dispatchEvent(new MouseEvent("click", { bubbles: true })); await Promise.resolve(); }); }

  it("shows named and derived card titles, summaries, trust, review metadata, and hides hashes by default", async () => {
    await render();
    expect(host.textContent).toContain("Development key");
    expect(host.textContent).toContain("serial 8");
    expect(host.textContent).toContain("OpenAPI Example");
    expect(host.textContent).toContain("Health check and asynchronous job API.");
    expect(host.textContent).toContain("No Name");
    expect(host.textContent).toContain("Reviewed by Reviewer on 2026-10-15");
    expect(host.textContent).not.toContain("fixture.old");
    const advanced = Array.from(host.querySelectorAll("details")).find((node) => node.textContent?.includes("Advanced"));
    expect(advanced?.open).toBe(false);
    expect(advanced?.querySelector("code")?.textContent).toBe("fixture.template-openapi");
  });

  it("shows the description as plain text in details and a fallback when absent", async () => {
    await render();
    await click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("OpenAPI Example")) ?? null);
    const dialog = host.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("First line.\nSecond line.");
    expect(dialog?.querySelector("p")?.innerHTML).not.toContain("<br");
    await click(dialog?.querySelector('[aria-label="Close dialog"]') ?? null);
    await click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("No Name")) ?? null);
    expect(host.textContent).toContain("No description provided by the catalogue.");
  });

  it("opens a file install dialog with permissions and endpoint reference when setup is required", async () => {
    await render();
    await click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Install") ?? null);
    expect(host.textContent).toContain("No code, data only");
    expect(host.textContent).toContain("Capabilities:");
    expect(host.querySelector("#catalogue-endpoint-ref")).not.toBeNull();
    expect(host.textContent).not.toContain("LYNEWRIGHT_INTERNAL_ENDPOINT");
  });

  it("shows package execution details, installs it, and does not request an endpoint ref", async () => {
    vi.mocked(catalogueApi.install).mockResolvedValue({ integration: {}, installed: true });
    await render();
    const card = Array.from(host.querySelectorAll("article")).find((node) => node.textContent?.includes("Native Python Example"));
    await click(Array.from(card?.querySelectorAll("button") ?? []).find((button) => button.textContent?.trim() === "Install") ?? null);
    expect(host.textContent).toContain("Runs in its own container");
    expect(host.querySelector("#catalogue-endpoint-ref")).toBeNull();
    await click(Array.from(host.querySelectorAll('[role="dialog"] button')).find((button) => button.textContent?.trim() === "Install") ?? null);
    expect(catalogueApi.install).toHaveBeenCalledWith("fixture.demo-package", "1.0.0", "");
  });

  it("shows installed provenance and a drift warning with its reason", async () => {
    await render();
    expect(host.textContent).toContain("Installed ✓");
    expect(host.textContent).toContain("Catalogue (dev key)");
    expect(host.textContent).toContain("Drift · Entry was yanked.");
  });

  it("requires an explicit repair action for an incomplete package installation", async () => {
    vi.mocked(catalogueApi.repair).mockResolvedValue({ integration: {}, repaired: true });
    await render();
    const card = Array.from(host.querySelectorAll("article")).find((node) => node.textContent?.includes("Incomplete package"));
    expect(card?.textContent).toContain("Installation incomplete");
    await click(Array.from(card?.querySelectorAll("button") ?? []).find((button) => button.textContent?.trim() === "Repair") ?? null);
    expect(host.textContent).toContain("verified wheel and runner build file will be downloaded again");
    await click(Array.from(host.querySelectorAll('[role="dialog"] button')).find((button) => button.textContent?.trim() === "Repair installation") ?? null);
    expect(catalogueApi.repair).toHaveBeenCalledWith("fixture.incomplete", "1.0.0");
  });

  it("filters by text, kind, and installed state", async () => {
    await render();
    const search = host.querySelector<HTMLInputElement>("#catalogue-search")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "native python"); search.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(host.textContent).toContain("Native Python Example");
    expect(host.textContent).not.toContain("OpenAPI Example");
    const kind = host.querySelector<HTMLSelectElement>("#catalogue-kind")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(kind, "package"); kind.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(host.textContent).toContain("Native Python Example");
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, ""); search.dispatchEvent(new Event("input", { bubbles: true })); });
    const installed = Array.from(host.querySelectorAll("label")).find((label) => label.textContent?.trim() === "Installed")?.querySelector<HTMLInputElement>("input");
    expect(installed).not.toBeNull();
    await act(async () => { installed!.click(); });
    expect(host.textContent).toContain("Installed example");
    expect(host.textContent).not.toContain("Native Python Example");
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "nothing matches"); search.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(host.textContent).toContain("No results");
  });

  it("shows an untrusted state with next steps when no catalogue keys are configured", async () => {
    vi.mocked(catalogueApi.get).mockResolvedValue({ status: { url: "", status: "untrusted", trusted_key_ids: [], dev_key_trusted: false, dev_key_warning: false, dev_key_warning_message: null, verified_key_id: null, serial: null, generated_at: null, fetched_at: null, last_error: "no catalogue keys configured" }, entries: [] });
    await render();
    expect(host.textContent).toContain("Untrusted");
    expect(host.textContent).toContain("Add an operator key to enable signed catalogue installs.");
    expect(host.textContent).toContain("How to add a key");
    expect(host.querySelector("#catalogue-search")).toBeNull();
  });

  it("shows empty and filtered-empty states", async () => {
    vi.mocked(catalogueApi.get).mockResolvedValue({ ...response(), entries: [] });
    await render();
    expect(host.textContent).toContain("No catalogue entries");
  });
});
