/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi, type IntegrationDraft } from "../../api/integrationManagementClient";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: {
    drafts: vi.fn(), getDraft: vi.fn(), createDraft: vi.fn(), updateDraft: vi.fn(),
    probeDraft: vi.fn(), enableDraft: vi.fn(), discardDraft: vi.fn(), downloadDraft: vi.fn(),
  },
}));
vi.mock("../../context/ConfirmContext", () => ({ confirmAction: vi.fn().mockResolvedValue(true) }));
vi.mock("../../context/ProjectContext", () => ({ useProject: () => ({ projectId: "prj_1" }) }));
const { default: IntegrationDraftSection } = await import("./IntegrationDraftSection");

const makeDraft = (values: Partial<IntegrationDraft> = {}): IntegrationDraft => ({
  id: "drf_1", tenant_id: "ten_1", project_id: "prj_1", source_kind: "openapi_url", source_ref: "https://api.example.test/openapi.yaml",
  source_message: "", draft_kind: "integration_file", draft_json: { runtime_kind: "openapi", operations: [{ operation_id: "getStatus", enabled: true }] },
  validation_errors: [], status: "draft", probe_result: {}, probe_operation_id: "", probe_hash_locked: false,
  created_by: "1", created_at: "now", updated_at: "now", enabled_integration_id: "", ...values,
});

function setup() {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host);
  return { host, root };
}

describe("IntegrationDraftSection", () => {
  beforeEach(() => { (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true; });
  afterEach(() => { vi.clearAllMocks(); document.body.innerHTML = ""; });

  it("renders validation errors and keeps enable disabled", async () => {
    const invalid = makeDraft({ validation_errors: ["operations.0.operation_id: is invalid"] });
    vi.mocked(integrationManagementApi.drafts).mockResolvedValue({ items: [invalid] });
    vi.mocked(integrationManagementApi.getDraft).mockResolvedValue(invalid);
    const { host, root } = setup();
    await act(async () => { root.render(<IntegrationDraftSection />); await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector("[data-testid='integration-draft-errors']")?.textContent).toContain("operations.0.operation_id");
    expect(host.querySelector<HTMLButtonElement>("button[aria-label='Enable integration']")?.disabled).toBe(true);
    act(() => root.unmount());
  });

  it("enables only after a successful probe", async () => {
    const initial = makeDraft();
    const probed = makeDraft({ status: "probed", probe_hash_locked: true, probe_operation_id: "getStatus", probe_result: { status: "passed" } });
    vi.mocked(integrationManagementApi.drafts).mockResolvedValue({ items: [initial] });
    vi.mocked(integrationManagementApi.getDraft).mockResolvedValue(initial);
    vi.mocked(integrationManagementApi.probeDraft).mockResolvedValue(probed);
    const { host, root } = setup();
    await act(async () => { root.render(<IntegrationDraftSection />); await Promise.resolve(); await Promise.resolve(); });
    const enable = () => host.querySelector<HTMLButtonElement>("button[aria-label='Enable integration']")!;
    expect(enable().disabled).toBe(true);
    const probe = [...host.querySelectorAll("button")].find((button) => button.textContent === "Run probe")!;
    await act(async () => { probe.click(); await Promise.resolve(); });
    expect(enable().disabled).toBe(false);
    const operationToggle = host.querySelector<HTMLInputElement>("input[name='draft-operation-0']")!;
    await act(async () => { operationToggle.click(); await Promise.resolve(); });
    expect(enable().disabled).toBe(true);
    act(() => root.unmount());
  });

  it("shows the source, review, probe, and enable steps in order", async () => {
    const initial = makeDraft();
    const probed = makeDraft({ status: "probed", probe_hash_locked: true, probe_operation_id: "getStatus", probe_result: { status: "passed" } });
    vi.mocked(integrationManagementApi.drafts).mockResolvedValue({ items: [] });
    vi.mocked(integrationManagementApi.createDraft).mockResolvedValue(initial);
    vi.mocked(integrationManagementApi.probeDraft).mockResolvedValue(probed);
    const { host, root } = setup();
    await act(async () => { root.render(<IntegrationDraftSection embedded />); await Promise.resolve(); });
    const steps = () => [...host.querySelectorAll<HTMLOListElement>(".u3-flow-steps li")].map((step) => step.textContent?.split(" · ").slice(1).join(" · "));
    const activeStep = () => host.querySelector(".u3-flow-steps [aria-current='step']")?.textContent;
    expect(steps()).toEqual(["Source", "Review", "Probe one operation", "Enable"]);
    expect(activeStep()).toContain("Source");

    const source = host.querySelector<HTMLInputElement>("#integration-draft-url")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(source, "https://api.example.test/openapi.yaml");
      source.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...host.querySelectorAll("button")].find((button) => button.textContent === "Create draft")!.click(); await Promise.resolve(); });
    expect(activeStep()).toContain("Review");
    expect(host.querySelector<HTMLButtonElement>("button[aria-label='Enable integration']")?.disabled).toBe(true);

    await act(async () => { [...host.querySelectorAll("button")].find((button) => button.textContent === "Run probe")!.click(); await Promise.resolve(); });
    expect(activeStep()).toContain("Enable");
    expect(host.querySelector<HTMLButtonElement>("button[aria-label='Enable integration']")?.disabled).toBe(false);
    act(() => root.unmount());
  });
});
