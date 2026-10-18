import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ROUTES, resolveRoute } from "./router";

describe("dashboard route components", () => {
  it("points every route to an existing page component", () => {
    for (const route of ROUTES) {
      expect(existsSync(resolve(process.cwd(), "src", route.componentFile)), `${route.path} to ${route.componentFile}`).toBe(true);
    }
  });
  it("has no duplicate paths and no business destinations", () => {
    expect(new Set(ROUTES.map((route) => route.path)).size).toBe(ROUTES.length);
    expect(ROUTES.map((route) => route.path)).toEqual([
      "/overview", "/lines", "/runs", "/reviews", "/integrations", "/catalogue", "/settings",
      "/settings/operator", "/settings/telegram", "/settings/logs", "/settings/users", "/settings/credentials", "/settings/media", "/account", "/mcp/oauth/consent",
    ]);
  });
  it("resolves the root to Overview for the post-login redirect", () => {
    expect(resolveRoute({ pathname: "/", search: "", hash: "" })).toMatchObject({ path: "/overview", page: "overview", label: "Overview" });
  });
  it("routes Catalogue as its own page", () => {
    expect(resolveRoute({ pathname: "/catalogue", search: "", hash: "" })).toMatchObject({ page: "catalogue", label: "Catalogue" });
  });
  it("routes Account and admin user management to their pages", () => {
    expect(resolveRoute({ pathname: "/account", search: "", hash: "" })).toMatchObject({ page: "account", label: "Account" });
    expect(resolveRoute({ pathname: "/settings/users", search: "", hash: "" })).toMatchObject({ page: "users", label: "Users" });
  });
  it("routes an integration id to the detail view", () => {
    expect(resolveRoute({ pathname: "/integrations/lynewright.integration.alpha", search: "", hash: "" }))
      .toMatchObject({ path: "/integrations", page: "integrations", params: { id: "lynewright.integration.alpha" } });
  });
  it("resolves integration sections from their path segment", () => {
    expect(resolveRoute({ pathname: "/integrations/lynewright.integration.alpha/openapi-mappings", search: "", hash: "" }))
      .toMatchObject({ path: "/integrations", page: "integrations", params: { id: "lynewright.integration.alpha", section: "openapi-mappings" } });
  });
});


describe("dev-only design route", () => {
  it("is available in development without entering the production route table", () => {
    expect(ROUTES.some((route) => route.path === "/design")).toBe(false);
    expect(resolveRoute({ pathname: "/design", search: "", hash: "" }).page).toBe("design");
  });
});
