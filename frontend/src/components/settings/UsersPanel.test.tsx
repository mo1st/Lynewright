/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: {
  listUsers: vi.fn(), createUser: vi.fn(), updateUser: vi.fn(), deleteUser: vi.fn(),
} }));
vi.mock("../../api/client", () => ({ api: apiMock, describeApiError: (error: unknown) => String(error) }));

const { UsersPanel } = await import("./UsersPanel");
const { ToastProvider } = await import("../ui/Feedback");

function setup() {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host);
  return { host, root };
}

describe("UsersPanel removal confirmation", () => {
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    apiMock.listUsers.mockResolvedValue([
      { id: 1, email: "admin@example.test", role: "admin", is_active: true, created_at: "2026-01-01T00:00:00Z" },
      { id: 2, email: "staff@example.test", role: "editor", is_active: true, created_at: "2026-01-02T00:00:00Z" },
    ]);
  });
  afterEach(() => { document.body.innerHTML = ""; });

  it("requires typing the user's email before delete can be confirmed", async () => {
    const { host, root } = setup();
    await act(async () => { root.render(<ToastProvider><UsersPanel currentUserId={1} /></ToastProvider>); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.getAttribute("aria-label") === "Delete staff@example.test")!.click(); });
    expect(host.textContent).toContain("Existing run history is kept.");
    const confirm = [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Delete user")!;
    expect(confirm.disabled).toBe(true);
    const input = host.querySelector<HTMLInputElement>("#managed-user-delete-confirm")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "staff@example.test");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(confirm.disabled).toBe(false);
    await act(async () => { confirm.click(); await Promise.resolve(); });
    expect(apiMock.deleteUser).toHaveBeenCalledWith(2);
    act(() => root.unmount());
  });
});
