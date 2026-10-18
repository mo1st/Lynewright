/** @vitest-environment jsdom */
import { afterEach, describe, expect, it } from "vitest";
import { navigate, registerNavigationGuard, resolveRoute } from "./router";

describe("Lynewright routes", () => {
  afterEach(() => { window.history.replaceState({}, "", "/lines"); });
  it.each([
    ["/lines", "lines"], ["/runs", "runs"], ["/reviews", "reviews"],
    ["/integrations", "integrations"], ["/settings", "settings"],
    ["/settings/operator", "operator"], ["/settings/telegram", "telegram"],
    ["/settings/logs", "logs"], ["/mcp/oauth/consent?request_id=r1", "oauth"],
    ["/settings/users", "users"], ["/account", "account"],
  ])("resolves %s", (url, page) => {
    const parsed = new URL(url, "http://lynewright.test");
    expect(resolveRoute({ pathname: parsed.pathname, search: parsed.search, hash: parsed.hash }).page).toBe(page);
  });
  it("resolves line and run detail paths and preserves list filters", () => {
    expect(resolveRoute({ pathname: "/lines/demo-line", search: "", hash: "" })).toMatchObject({ page: "lines", params: { id: "demo-line" } });
    expect(resolveRoute({ pathname: "/runs/run-123", search: "", hash: "" })).toMatchObject({ page: "runs", params: { id: "run-123" } });
    expect(resolveRoute({ pathname: "/runs", search: "?status=failed&line=demo-line", hash: "" }).params).toMatchObject({ status: "failed", line: "demo-line" });
  });
  it("resolves an integration detail section from the path", () => {
    expect(resolveRoute({ pathname: "/integrations/lynewright.integration.alpha/health", search: "", hash: "" }))
      .toMatchObject({ page: "integrations", params: { id: "lynewright.integration.alpha", section: "health" } });
  });
  it("blocks a guarded route change and allows it after unregistering", () => {
    window.history.replaceState({}, "", "/lines");
    const unregister = registerNavigationGuard(() => false);
    navigate("/integrations");
    expect(window.location.pathname).toBe("/lines");
    unregister();
    navigate("/integrations");
    expect(window.location.pathname).toBe("/integrations");
  });
});
