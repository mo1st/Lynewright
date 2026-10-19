/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { interactionMock, operatorMock, confirmMock } = vi.hoisted(() => ({
  interactionMock: {
    telegramSettings: vi.fn(), listConnections: vi.fn(), listNotificationRoutes: vi.fn(), listLinks: vi.fn(),
    createConnection: vi.fn(), deleteConnection: vi.fn(), createLinkToken: vi.fn(), linkTokenStatus: vi.fn(),
    upsertNotificationRoute: vi.fn(), sendTestNotification: vi.fn(), revokeLink: vi.fn(), setDeliveryMode: vi.fn(),
    updateConnection: vi.fn(),
  },
  operatorMock: { listProfiles: vi.fn(), updateProfile: vi.fn() },
  confirmMock: vi.fn(),
}));
vi.mock("../../api/operatorClient", () => ({ interactionApi: interactionMock, operatorApi: operatorMock }));
vi.mock("../../api/client", () => ({ describeApiError: (error: unknown, fallback: string) => (error instanceof Error ? error.message : fallback) }));
vi.mock("../../context/ConfirmContext", () => ({ confirmAction: confirmMock }));
vi.mock("../../context/ProjectContext", () => ({ useProject: () => ({ projectId: "demo-project", loading: false }) }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,QR") } }));

const { TelegramOperatorPanel } = await import("./TelegramOperatorPanel");

const BOT = {
  id: "ic_1", project_id: "demo-project", channel_type: "telegram", name: "Telegram bot",
  external_identity: "@lyne_bot", operator_profile_id: "", enabled: true, delivery_mode: "polling",
  last_update_at: null, last_error: "",
};
const LINK = {
  id: "link_1", project_id: "demo-project", user_id: 1, connection_id: "ic_1", channel_type: "telegram",
  external_user_id: "555", external_username: "owner_tg", status: "active", created_at: "2026-10-19T10:00:00Z",
};

async function flush(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

async function render(): Promise<{ host: HTMLDivElement; root: Root }> {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(<TelegramOperatorPanel />); await flush(); });
  return { host, root };
}

function button(host: HTMLElement, label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === label);
  if (!found) throw new Error(`button ${label} not found`);
  return found;
}

describe("TelegramOperatorPanel", () => {
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    interactionMock.telegramSettings.mockResolvedValue({ enabled: true, webhook_available: false, public_url: "http://localhost:8080" });
    interactionMock.listConnections.mockResolvedValue([]);
    interactionMock.listNotificationRoutes.mockResolvedValue([]);
    interactionMock.listLinks.mockResolvedValue([]);
    operatorMock.listProfiles.mockResolvedValue([]);
    confirmMock.mockResolvedValue(true);
  });
  afterEach(() => { document.body.innerHTML = ""; });

  it("guides a new user without any webhook or terminal step", async () => {
    const { host, root } = await render();
    expect(host.textContent).toContain("Create your bot");
    expect(host.textContent).toContain("@BotFather");
    expect(host.textContent).not.toMatch(/webhook|secret|curl/i);
    interactionMock.createConnection.mockResolvedValue({ ...BOT, bot_username: "lyne_bot", webhook_secret: "x" });
    interactionMock.listConnections.mockResolvedValue([BOT]);
    const input = host.querySelector<HTMLInputElement>("#telegram-bot-token")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "123:abc");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { button(host, "Connect").click(); await flush(); });
    expect(interactionMock.createConnection).toHaveBeenCalledWith({ project_id: "demo-project", name: "Telegram bot", bot_token: "123:abc" });
    expect(host.textContent).toContain("Bot connected: @lyne_bot");
    expect(host.textContent).toContain("Link my Telegram");
    act(() => root.unmount());
  });

  it("removes a connection Telegram rejected and explains why", async () => {
    interactionMock.createConnection.mockResolvedValue({ ...BOT, external_identity: "", bot_username: "", webhook_secret: "x" });
    const { host, root } = await render();
    const input = host.querySelector<HTMLInputElement>("#telegram-bot-token")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "wrong");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { button(host, "Connect").click(); await flush(); });
    expect(interactionMock.deleteConnection).toHaveBeenCalledWith("ic_1");
    expect(host.textContent).toContain("Telegram did not accept that token");
    act(() => root.unmount());
  });

  it("links with one tap and turns green when Telegram confirms", { timeout: 10000 }, async () => {
    interactionMock.listConnections.mockResolvedValue([BOT]);
    interactionMock.createLinkToken.mockResolvedValue({
      id: "ilt_1", token: "abc", connection_id: "ic_1", project_id: "demo-project", expires_at_epoch: 0, ttl_seconds: 600,
      deep_link: "https://t.me/lyne_bot?start=abc",
    });
    interactionMock.linkTokenStatus.mockResolvedValue({ id: "ilt_1", linked: true, expired: false, external_username: "owner_tg" });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { host, root } = await render();
    await act(async () => { button(host, "Link my Telegram").click(); await flush(); });
    expect(interactionMock.createLinkToken).toHaveBeenCalledWith({ connection_id: "ic_1", project_id: "demo-project", ttl_seconds: 600 });
    await act(async () => { button(host, "Open in Telegram").click(); });
    expect(open).toHaveBeenCalledWith("https://t.me/lyne_bot?start=abc", "_blank", "noopener,noreferrer");
    expect(host.textContent).toContain("Waiting for Telegram");
    interactionMock.listLinks.mockResolvedValue([LINK]);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2300)); await flush(); });
    expect(host.textContent).toContain("Linked to @owner_tg");
    expect(host.textContent).toContain("Tell me when");
    act(() => root.unmount());
  });

  it("toggles notifications as routes and reports a delivered test", async () => {
    interactionMock.listConnections.mockResolvedValue([BOT]);
    interactionMock.listLinks.mockResolvedValue([LINK]);
    interactionMock.upsertNotificationRoute.mockImplementation(async (payload: { event_class: string; intent: string }) => ({
      id: "nr_1", project_id: "demo-project", event_class: payload.event_class, intent: payload.intent, channels: [{ channel: "telegram" }], enabled: true,
    }));
    interactionMock.sendTestNotification.mockResolvedValue({ results: [{ channel: "telegram", status: "sent" }] });
    const { host, root } = await render();
    const failed = host.querySelector<HTMLInputElement>('input[name="telegram-run_failed"]')!;
    expect(failed.checked).toBe(true);
    await act(async () => { failed.click(); await flush(); });
    expect(interactionMock.upsertNotificationRoute).toHaveBeenCalledWith(expect.objectContaining({ event_class: "run_failed", intent: "SILENT" }));
    expect(host.querySelector<HTMLInputElement>('input[name="telegram-run_failed"]')!.checked).toBe(false);
    await act(async () => { button(host, "Send test message").click(); await flush(); });
    expect(host.textContent).toContain("Test message sent");
    expect(button(host, "Use webhook (faster)").disabled).toBe(true);
    act(() => root.unmount());
  });

  it("says plainly when the server has Telegram turned off", async () => {
    interactionMock.telegramSettings.mockResolvedValue({ enabled: false, webhook_available: false, public_url: "" });
    const { host, root } = await render();
    expect(host.textContent).toContain("Telegram is turned off on this server");
    act(() => root.unmount());
  });
});
