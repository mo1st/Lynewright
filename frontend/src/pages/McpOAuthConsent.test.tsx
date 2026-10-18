/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { api, setSessionToken } = vi.hoisted(() => ({
  api: { getMcpOAuthRequest: vi.fn(), decideMcpOAuthRequest: vi.fn() },
  setSessionToken: vi.fn(),
}));
vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, api, setSessionToken };
});

const { default: McpOAuthConsent } = await import("./McpOAuthConsent");

describe("MCP OAuth consent page", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.appendChild(host);
    api.getMcpOAuthRequest.mockResolvedValue({
      request_id: "qmr_test",
      application_name: "Unverified Client Name",
      redirect_origin: "https://client.example.test",
      requested_scopes: ["system:read", "projects:create"],
      default_scopes: ["system:read"],
      account_email: "admin@example.test",
      expires_at: Date.now() / 1000 + 300,
    });
    api.decideMcpOAuthRequest.mockResolvedValue({ redirect_to: "https://client.example.test/callback?code=redacted" });
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("shows the platform-wide disclosure, account, callback, and read defaults", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<McpOAuthConsent requestId="qmr_test" />);
      await Promise.resolve();
    });

    expect(host.textContent).toContain("Unverified Client Name");
    expect(host.textContent).toContain("https://client.example.test");
    expect(host.textContent).toContain("admin@example.test");
    expect(host.textContent).toContain("Access applies across the Lynewright platform");
    expect(host.textContent).toContain("not a verified identity");
    const scopes = host.querySelectorAll<HTMLInputElement>(".mcp-oauth-permissions input[type=checkbox]");
    expect(scopes[0].checked).toBe(true);
    expect(scopes[1].checked).toBe(false);
    expect(host.textContent).toContain("Allow access");
    expect(host.textContent).toContain("Cancel");
  });

  it("requires a fresh explicit confirmation before enabling write access", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<McpOAuthConsent requestId="qmr_test" />);
      await Promise.resolve();
    });

    const writeScope = host.querySelectorAll<HTMLInputElement>(".mcp-oauth-permissions input[type=checkbox]")[1];
    await act(async () => writeScope.click());
    const allow = Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("Allow access"));
    expect(host.textContent).toContain("can change data or settings across the platform");
    expect(allow?.disabled).toBe(true);
    expect(api.decideMcpOAuthRequest).not.toHaveBeenCalled();

    const confirmation = host.querySelector<HTMLInputElement>(".mcp-oauth-write-confirm input");
    expect(confirmation).not.toBeNull();
    await act(async () => confirmation!.click());
    expect(allow?.disabled).toBe(false);

    await act(async () => writeScope.click());
    await act(async () => writeScope.click());
    expect(host.querySelector<HTMLInputElement>(".mcp-oauth-write-confirm input")?.checked).toBe(false);
    expect(allow?.disabled).toBe(true);
  });
});
