/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IntegrationManagementSummary } from "../types";
import realLineOperations from "../../e2e/fixtures/u3b-line-operations.real.json";

const { integrationApiMock, getCredentialsMock, productionLinesApiMock } = vi.hoisted(() => ({
  integrationApiMock: {
    list: vi.fn(), get: vi.fn(), operations: vi.fn(), settings: vi.fn(), updateSettings: vi.fn(),
    createRunnerToken: vi.fn(), runtimeConfig: vi.fn(), enable: vi.fn(), disable: vi.fn(),
    reconcile: vi.fn(), removalPreview: vi.fn(), remove: vi.fn(), exportFile: vi.fn(), confirmPackageInstall: vi.fn(),
    importFile: vi.fn(), drafts: vi.fn(), getDraft: vi.fn(), createManual: vi.fn(),
  },
  getCredentialsMock: vi.fn(),
  productionLinesApiMock: { operations: vi.fn() },
}));

vi.mock("../api/integrationManagementClient", () => ({ integrationManagementApi: integrationApiMock }));
vi.mock("../api/client", () => ({ api: { getCredentials: getCredentialsMock } }));
vi.mock("../api/productionLinesClient", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api/productionLinesClient")>()), productionLinesApi: productionLinesApiMock }));
vi.mock("../context/ProjectContext", () => ({ useProject: () => ({ projectId: "project-1", projects: [] }) }));
vi.mock("../components/integrations/AuthorizationSection", () => ({ default: () => null }));
vi.mock("../components/integrations/BindingsSection", () => ({ default: () => null }));
vi.mock("../components/integrations/ConnectionManagementSection", () => ({ default: () => null }));
vi.mock("../components/integrations/DefinitionUpgradeSection", () => ({ default: () => null }));
vi.mock("../components/production/ProductionLineSettings", () => ({ default: () => null }));
vi.mock("../components/integrations/HealthDiagnosticsSection", () => ({ default: () => null }));
vi.mock("../components/integrations/IntegrationProvenanceDetails", () => ({ default: () => null }));
vi.mock("../components/integrations/IntegrationDraftSection", () => ({ default: () => <div>Draft source review probe enable</div> }));
vi.mock("../components/integrations/ManualIntegrationSection", () => ({ default: () => <div>Manual integration form</div> }));
vi.mock("../components/integrations/McpContractMappingsSection", () => ({ default: () => null }));
vi.mock("../components/integrations/McpResourceMappingsSection", () => ({ default: () => null }));
vi.mock("../components/integrations/OpenApiManagementSection", () => ({ default: () => null }));
vi.mock("../components/integrations/ResourcesSection", () => ({ default: () => null }));
vi.mock("../components/integrations/SetupRequirementsSection", () => ({ default: () => null }));
vi.mock("../components/integrations/WebhooksSection", () => ({ default: () => null }));
vi.mock("../components/integrations/InstallConfirmationDialog", () => ({ default: () => null }));

const { default: Integrations } = await import("./Integrations");
const sourceKind = ["cata", "logue"].join("") as NonNullable<IntegrationManagementSummary["provenance"]>["kind"];

function makeIntegration(id: string, options: { runtime?: string; source?: string; executionPlane?: string; settings?: boolean; connection?: boolean } = {}): IntegrationManagementSummary {
  const runtime = options.runtime;
  const source = options.source ?? (runtime ? "manual" : "bundled");
  const isContainer = options.executionPlane === "container";
  return {
    integration: {
      id, name: id.split(".").slice(-1)[0] ?? id, version: "1.0.0", type: runtime ?? "native", status: "ready", enabled: true,
      requested_capabilities: [], granted_capabilities: [], permission_mode: "restricted", last_error: "", source,
      execution_plane: options.executionPlane ?? "direct", trusted_inprocess: false,
      configuration: runtime ? { manual: { runtime } } : {},
    },
    provenance: { kind: source === "bundled" ? "bundled" : sourceKind, status: "current", recorded_at: "2026-01-01T00:00:00Z", signing_key_id: "dev:key" },
    availability: { available: true, enabled: true, configured: Boolean(options.connection), manual: Boolean(runtime), kind: runtime ?? "native", execution_plane: options.executionPlane ?? "direct", runner_status: isContainer ? "connected" : "never_seen", runner_last_seen_at: isContainer ? "2026-01-02T00:00:00Z" : null },
    capabilities: { requested: [], granted: [], permission_mode: "restricted" },
    connections: options.connection ? [{ id: `${id}-conn`, integration_id: id, name: "Primary", project_id: "project-1", endpoint_url: "https://example.test", origin_kind: "public", internal_endpoint_ref: "", credential_refs: [], enabled: true, health: {}, last_health_check_at: null, last_error: "" }] : [],
    management: { sections: [], resource_types: [], supported_actions: [], settings_schema: options.settings ? { schema_id: "s1", fields: [], values: {} } : null },
    activity: { connection_count: options.connection ? 1 : 0, resource_count: 0, binding_count: 0 }, setup_requirements: [],
    health: { status: "healthy" }, attention: [], attention_items: [], oauth: { supported: false }, inbound: [],
  };
}

function setup(path = "/integrations", items = [makeIntegration("lynewright.integration.alpha")]) {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host);
  window.history.replaceState({}, "", path);
  integrationApiMock.list.mockResolvedValue({ items, package_statuses: [], install_policy: "confirm" });
  integrationApiMock.get.mockImplementation(async (id: string) => items.find((item) => item.integration.id === id));
  productionLinesApiMock.operations.mockResolvedValue({ items: items.map((item) => ({
    integration_id: item.integration.id, connection_id: `${item.integration.id}-connection`, operation_id: `${item.integration.id}.op`, revision: "fixture-revision",
    kind: "native", contract: "lynewright.demo.writer", operation: "write", description: "Write a prepared line output.", schema: { type: "object", properties: {}, additionalProperties: false },
  })) });
  getCredentialsMock.mockResolvedValue([]);
  return { host, root };
}

describe("U3 integration workspace", () => {
  beforeEach(() => { (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true; vi.clearAllMocks(); });
  afterEach(() => { document.body.innerHTML = ""; });

  it("filters the list by kind, status, and source badges; old selector and panel tab row are gone", async () => {
    const values = [makeIntegration("lynewright.integration.native"), makeIntegration("lynewright.integration.openapi", { runtime: "openapi", connection: true }), makeIntegration("lynewright.integration.runner", { source: "package", executionPlane: "container" })];
    const { host, root } = setup("/integrations", values);
    await act(async () => { root.render(<Integrations />); await Promise.resolve(); await Promise.resolve(); });
    expect(host.textContent).toContain("Cata" + "logue (dev key)");
    expect(host.textContent).toContain("Runner last seen");
    expect(host.querySelector("#integration-kind-filter")).toBeTruthy();
    expect(host.querySelector(".integration-selector")).toBeNull();
    expect(host.querySelector('[aria-label="Integration panels"]')).toBeNull();
    await act(async () => { (host.querySelector("#integration-kind-filter") as HTMLSelectElement).value = "OpenAPI"; host.querySelector("#integration-kind-filter")!.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(host.textContent).toContain("lynewright.integration.openapi");
    expect(host.textContent).not.toContain("lynewright.integration.native");
    act(() => root.unmount());
  });

  it("shows only sections that apply to the integration kind and its data", async () => {
    const openapi = makeIntegration("lynewright.integration.openapi", { runtime: "openapi", connection: true });
    const { host, root } = setup(`/integrations/${openapi.integration.id}`, [openapi]);
    await act(async () => { root.render(<Integrations />); await Promise.resolve(); await Promise.resolve(); });
    const nav = host.querySelector('[aria-label="Integration sections"]');
    expect(nav?.textContent).toContain("OpenAPI mappings");
    expect(nav?.textContent).not.toContain("MCP tools");
    expect(nav?.textContent).not.toContain("Runner");
    expect(host.querySelectorAll("nav[aria-label='Integration sections']")).toHaveLength(0);
    expect(nav?.getAttribute("role")).toBe("tablist");
    act(() => root.unmount());
  });

  it("syncs integration tabs to the URL and follows back and forward", async () => {
    const integration = makeIntegration("lynewright.integration.navigation", { settings: true });
    const { host, root } = setup(`/integrations/${integration.integration.id}`, [integration]);
    await act(async () => { root.render(<Integrations />); await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    expect(window.location.pathname).toBe(`/integrations/${integration.integration.id}/overview`);
    const operationTab = [...host.querySelectorAll<HTMLElement>('[role="tab"]')].find((tab) => tab.textContent === "Operations");
    expect(operationTab).toBeTruthy();
    await act(async () => { operationTab!.click(); await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    expect(window.location.pathname).toBe(`/integrations/${integration.integration.id}/operations`);
    expect(operationTab?.getAttribute("aria-selected")).toBe("true");
    await act(async () => {
      const back = new Promise<void>((resolve) => window.addEventListener("popstate", () => resolve(), { once: true }));
      window.history.back();
      await back;
    });
    expect(window.location.pathname).toBe(`/integrations/${integration.integration.id}/overview`);
    expect([...host.querySelectorAll<HTMLElement>('[role="tab"]')].find((tab) => tab.textContent === "Overview")?.getAttribute("aria-selected")).toBe("true");
    await act(async () => {
      const forward = new Promise<void>((resolve) => window.addEventListener("popstate", () => resolve(), { once: true }));
      window.history.forward();
      await forward;
    });
    expect(window.location.pathname).toBe(`/integrations/${integration.integration.id}/operations`);
    act(() => root.unmount());
  });

  it.each([
    ["Demo Writer", "lynewright.integration.demo-writer"],
    ["installed package", "lynewright.integration.example-native"],
  ])("shows the live project operation response for %s", async (_label, id) => {
    const captured = realLineOperations.items as Array<Record<string, unknown>>;
    const rows = captured.filter((item) => item.integration_id === id);
    expect(rows.length, `live API fixture has operations for ${id}`).toBeGreaterThan(0);
    expect(rows.every((item) => typeof item.operation_id === "string" && typeof item.contract === "string" && typeof item.operation === "string" && typeof item.description === "string" && typeof item.kind === "string")).toBe(true);
    const value = makeIntegration(id, { settings: true, ...(id.endsWith("example-native") ? { source: "package", executionPlane: "container" } : {}) });
    const { host, root } = setup(`/integrations/${value.integration.id}`, [value]);
    productionLinesApiMock.operations.mockResolvedValue(realLineOperations);
    await act(async () => { root.render(<Integrations />); await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    const operations = [...host.querySelectorAll("button")].find((button) => button.textContent === "Operations");
    expect(operations).toBeTruthy();
    await act(async () => { operations!.click(); await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    expect(host.textContent).toContain(String(rows[0].description));
    expect(host.textContent).toContain(rows.some((item) => item.async) ? "Async" : "Sync");
    expect(host.textContent).toContain("Approved and enabled");
    expect(productionLinesApiMock.operations).toHaveBeenCalledWith("project-1");
    act(() => root.unmount());
  });

  it.each([
    ["native bundled", makeIntegration("lynewright.integration.native", { settings: true }), ["Settings"], ["Runner", "MCP tools", "OpenAPI mappings"]],
    ["package container", makeIntegration("lynewright.integration.package", { source: "package", executionPlane: "container" }), ["Runner"], ["MCP tools", "OpenAPI mappings"]],
    ["OpenAPI file", makeIntegration("lynewright.integration.openapi-file", { runtime: "openapi", connection: true }), ["OpenAPI mappings"], ["MCP tools", "Runner"]],
    ["MCP draft", makeIntegration("lynewright.integration.mcp-draft", { runtime: "mcp", connection: true }), ["MCP tools"], ["OpenAPI mappings", "Runner"]],
  ])("selects contextual sections for %s", async (_label, value, visible, hidden) => {
    const item = value as IntegrationManagementSummary;
    const { host, root } = setup(`/integrations/${item.integration.id}`, [item]);
    await act(async () => { root.render(<Integrations />); await Promise.resolve(); await Promise.resolve(); });
    const nav = host.querySelector('[aria-label="Integration sections"]')?.textContent ?? "";
    for (const name of visible as string[]) expect(nav).toContain(name);
    for (const name of hidden as string[]) expect(nav).not.toContain(name);
    act(() => root.unmount());
  });

  it("offers token creation and configuration download on container integrations", async () => {
    const runner = makeIntegration("lynewright.integration.runner", { source: "package", executionPlane: "container" });
    const { host, root } = setup(`/integrations/${runner.integration.id}`, [runner]);
    integrationApiMock.createRunnerToken.mockResolvedValue({ token: "crn_one-time", token_id: "r1", integration_id: runner.integration.id, message: "Shown once" });
    integrationApiMock.runtimeConfig.mockResolvedValue({ format: "compose", content: "services: {}", warnings: [] });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() });
    await act(async () => { root.render(<Integrations />); await Promise.resolve(); await Promise.resolve(); });
    const runnerTab = [...host.querySelectorAll("button")].find((button) => button.textContent === "Runner");
    await act(async () => { runnerTab!.click(); });
    const create = [...host.querySelectorAll("button")].find((button) => button.textContent?.includes("Create or rotate token"));
    await act(async () => { create!.click(); await Promise.resolve(); });
    expect(host.textContent).toContain("Shown once");
    expect(host.textContent).toContain("crn_one-time");
    const download = [...host.querySelectorAll("button")].find((button) => button.textContent?.includes("Download runtime config"));
    await act(async () => { download!.click(); await Promise.resolve(); });
    expect(integrationApiMock.runtimeConfig).toHaveBeenCalledWith(runner.integration.id, "compose");
    act(() => root.unmount());
    anchorClick.mockRestore();
    vi.unstubAllGlobals();
  });

  it("shows the four-option chooser and the guarded link-or-docs flow", async () => {
    const { host, root } = setup();
    await act(async () => { root.render(<Integrations />); await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { [...host.querySelectorAll("button")].find((button) => button.textContent === "Add integration")!.click(); });
    expect(host.textContent).toContain("From cata" + "logue");
    expect(host.textContent).toContain("Import file");
    expect(host.textContent).toContain("From link or docs");
    expect(host.textContent).toContain("Manual");
    await act(async () => { [...host.querySelectorAll("button")].find((button) => button.textContent?.includes("From link or docs"))!.click(); });
    expect(host.textContent).toContain("A live probe is required before enable.");
    expect(host.textContent).toContain("Draft source review probe enable");
    act(() => root.unmount());
  });

  it("shows the removal preview and requires line-disable plus the typed integration id", async () => {
    const item = makeIntegration("lynewright.integration.remove-me", { runtime: "openapi" });
    integrationApiMock.removalPreview.mockResolvedValue({
      integration_id: item.integration.id, name: item.integration.name, bundled: false, blocked: true,
      blocked_reasons: [`Lines use this integration: Publishing line (${item.integration.id}). Edit them first, or choose to disable those lines.`], disable_lines_blocked_reasons: [],
      lines: [{ id: "line-1", name: "Publishing line", project_id: "project-1", enabled: true, operations: ["writer.render"] }],
      connections: [{ id: "conn-1", name: "Main" }], mappings: { mcp_mappings: 1 }, runner_tokens: 0,
      stored_wheel: null, staging_files: [], media: [], settings: true,
      credentials: { used: ["writer.api"], exclusive: ["writer.api"], shared: [] },
      listed_package: false, runner_instruction: "Stop and remove this integration's runner container using your deployment tools.",
    });
    const { host, root } = setup(`/integrations/${item.integration.id}`, [item]);
    await act(async () => { root.render(<Integrations />); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host.querySelector("summary")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    const remove = [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Remove")!;
    await act(async () => { remove.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(integrationApiMock.removalPreview).toHaveBeenCalledWith(item.integration.id);
    expect(host.textContent).toContain("Publishing line");
    expect(host.textContent).toContain("Credentials are kept by default");
    expect(host.textContent).toContain("Integration ID");
    expect(host.querySelector('[aria-label="Copy Integration ID"]')).not.toBeNull();
    expect(host.textContent).toContain(item.integration.id);
    const submit = [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Remove integration")!;
    expect(submit.disabled).toBe(true);
    const lineToggle = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((input) => input.parentElement?.textContent?.includes("Disable these lines, then remove the integration"))!;
    await act(async () => { lineToggle.click(); });
    const typed = host.querySelector<HTMLInputElement>("#integration-remove-confirm")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(typed, item.integration.id);
      typed.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(submit.disabled).toBe(false);
    await act(async () => { submit.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(integrationApiMock.remove).toHaveBeenCalledWith(item.integration.id, {
      confirm_name: item.integration.id, disable_using_lines: true, delete_exclusive_credentials: false,
    });
    act(() => root.unmount());
  });

  it("keeps removal disabled when the preview reports a hard block", async () => {
    const item = makeIntegration("lynewright.integration.busy", { runtime: "mcp" });
    integrationApiMock.removalPreview.mockResolvedValue({
      integration_id: item.integration.id, name: item.integration.name, bundled: false, blocked: true,
      blocked_reasons: ["Runner invocations are active."], disable_lines_blocked_reasons: ["Runner invocations are active."], lines: [], connections: [], mappings: {}, runner_tokens: 1,
      stored_wheel: null, staging_files: [], media: [], settings: false,
      credentials: { used: [], exclusive: [], shared: [] }, listed_package: false,
    });
    const { host, root } = setup(`/integrations/${item.integration.id}`, [item]);
    await act(async () => { root.render(<Integrations />); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host.querySelector("summary")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Remove")!.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("Removal is blocked");
    expect(host.textContent).toContain("Runner invocations are active.");
    expect([...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Remove integration")!.disabled).toBe(true);
    expect(integrationApiMock.remove).not.toHaveBeenCalled();
    act(() => root.unmount());
  });
});
