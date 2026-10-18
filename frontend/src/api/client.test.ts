/** @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";

const { api, setActiveProjectId, setSessionToken } = await import("./client");

describe("shared project request scope", () => {
  afterEach(() => {
    setActiveProjectId("");
    setSessionToken("");
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("attaches the active project header after project selection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setActiveProjectId("demo-project");

    await api.getApplicationLogs();

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("X-Project-ID")).toBe("demo-project");
  });

  it("does not report a frontend error until a project scope exists", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await api.reportFrontendLog({ level: "error", category: "test", message: "startup failure" });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps the dashboard session in the bearer header for OAuth consent decisions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ redirect_to: "https://client.example.test/callback" }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    setSessionToken("dashboard-session-secret");

    await api.decideMcpOAuthRequest("qmr_safe-request", {
      approved: true, scopes: ["system:read"], confirm_writes: false,
    });

    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/mcp-oauth/requests/qmr_safe-request/decision");
    expect(url).not.toContain("dashboard-session-secret");
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer dashboard-session-secret");
    expect(String(request.body)).not.toContain("dashboard-session-secret");
  });
});
