/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, confirmMock } = vi.hoisted(() => ({
  apiMock: { listPlatformMcpClients: vi.fn(), createPlatformMcpClient: vi.fn(), deletePlatformMcpClient: vi.fn(), listAutomationPlans: vi.fn(), authorizeAutomationPlan: vi.fn() },
  confirmMock: vi.fn(),
}));
vi.mock("../../api/client", () => ({ api: apiMock, describeApiError: (error: unknown) => String(error) }));
vi.mock("../../context/ConfirmContext", () => ({ confirmAction: confirmMock }));

const { AssistantsPanel } = await import("./AssistantsPanel");
const { ToastProvider } = await import("../ui/Feedback");

function setup() {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host);
  return { host, root };
}

describe("AssistantsPanel", () => {
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    apiMock.listPlatformMcpClients.mockResolvedValue({ clients: [], mcp_url: "http://assist.example.test/mcp" });
    apiMock.listAutomationPlans.mockResolvedValue({ items: [] });
    apiMock.createPlatformMcpClient.mockResolvedValue({
      id: "client-1", name: "Studio assistant", type: "mcp", permission_mode: "custom",
      granted_capabilities: ["lines:read", "runs:start"], enabled: true, created_at: "2026-10-18T12:00:00Z",
      updated_at: "2026-10-18T12:00:00Z", last_authenticated_at: null, tenant_id: "ten_default",
      project_ids: ["demo-project"], on_behalf_of: "admin@example.test", access_token: "qxc-one-time-token",
    });
    apiMock.deletePlatformMcpClient.mockResolvedValue(undefined);
    confirmMock.mockResolvedValue(true);
  });
  afterEach(() => { document.body.innerHTML = ""; });

  it("creates a scoped client and shows its token once with the MCP URL", async () => {
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><AssistantsPanel projectId="demo-project" /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("No assistants connected");
    expect(host.textContent).toContain("http://assist.example.test/mcp");
    expect(host.textContent).toContain("HTTPS needed for web assistants");
    expect(host.querySelector('button[aria-label="Copy MCP connection URL"]')).not.toBeNull();
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Add assistant")!.click(); });
    const input = host.querySelector<HTMLInputElement>("#assistant-client-name")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "Studio assistant");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Create client")!.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(apiMock.createPlatformMcpClient).toHaveBeenCalledWith(expect.objectContaining({
      name: "Studio assistant", client_type: "mcp", permission_mode: "custom",
      granted_capabilities: expect.arrayContaining(["lines:read", "runs:read", "runs:start", "reviews:read"]),
    }));
    expect(host.textContent).toContain("Assistant connected");
    expect(host.textContent).toContain("qxc-one-time-token");
    expect(host.textContent).toContain("http://assist.example.test/mcp");
    act(() => root.unmount());
  });

  it("omits the HTTPS warning for the configured secure URL", async () => {
    apiMock.listPlatformMcpClients.mockResolvedValue({ clients: [], mcp_url: "https://assist.example.test/mcp" });
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><AssistantsPanel projectId="demo-project" /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("https://assist.example.test/mcp");
    expect(host.textContent).not.toContain("HTTPS needed for web assistants");
    act(() => root.unmount());
  });

  it("removes a client after the danger confirmation", async () => {
    apiMock.listPlatformMcpClients.mockResolvedValue({ clients: [{
      id: "client-1", name: "Claude", type: "mcp", permission_mode: "custom", granted_capabilities: ["lines:read"],
      enabled: true, created_at: "2026-10-18T12:00:00Z", updated_at: "2026-10-18T12:00:00Z",
      last_authenticated_at: null, tenant_id: "ten_default", project_ids: ["demo-project"], on_behalf_of: "admin@example.test",
    }], mcp_url: "https://assist.example.test/mcp" });
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><AssistantsPanel projectId="demo-project" /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Remove")!.click(); await Promise.resolve(); });
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ tone: "danger", confirmLabel: "Remove assistant" }));
    expect(apiMock.deletePlatformMcpClient).toHaveBeenCalledWith("client-1");
    act(() => root.unmount());
  });

  it("requires confirmation and approves the displayed revision and hash", async () => {
    const plan = {
      id: "apl_example", project_id: "demo-project", revision: 2, plan_hash: "a".repeat(64),
      status: "awaiting_authorization", objective: "Research topics", constraints: {},
      expires_at_epoch: Date.now() / 1000 + 3600,
      line_preview: { id: "preview-example-2", name: "Research preview", enabled: false, persisted: false, definition: { stages: [] } },
      operation_snapshots: [], authorization: { approved: false, approved_by: "" },
    };
    apiMock.listAutomationPlans.mockResolvedValue({ items: [plan] });
    apiMock.authorizeAutomationPlan.mockResolvedValue({ ...plan, authorization: { approved: true, approved_by: "admin@example.test" } });
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><AssistantsPanel projectId="demo-project" /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(apiMock.authorizeAutomationPlan).not.toHaveBeenCalled();
    confirmMock.mockResolvedValueOnce(false);
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Approve revision")!.click(); await Promise.resolve(); });
    expect(apiMock.authorizeAutomationPlan).not.toHaveBeenCalled();
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Approve revision")!.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(apiMock.authorizeAutomationPlan).toHaveBeenCalledWith(plan.id, { project_id: "demo-project", revision: 2, plan_hash: plan.plan_hash });
    expect(host.textContent).toContain("Approved by admin@example.test");
    act(() => root.unmount());
  });

  it("does not show approval success when a stale revision is rejected", async () => {
    apiMock.listAutomationPlans.mockResolvedValue({ items: [{
      id: "apl_stale", project_id: "demo-project", revision: 1, plan_hash: "b".repeat(64),
      status: "awaiting_authorization", objective: "Research topics", constraints: {},
      expires_at_epoch: Date.now() / 1000 + 3600,
      line_preview: { name: "Stale preview", enabled: false, persisted: false, definition: {} },
      operation_snapshots: [], authorization: { approved: false, approved_by: "" },
    }] });
    apiMock.authorizeAutomationPlan.mockRejectedValue(new Error("Plan changed; review the current revision"));
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><AssistantsPanel projectId="demo-project" /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Approve revision")!.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("Plan changed; review the current revision");
    expect(host.textContent).not.toContain("Approved by");
    act(() => root.unmount());
  });
});
