import { describe, expect, it } from "vitest";
import { ADMIN_SETTINGS_NAV, PRIMARY_NAV, SETTINGS_NAV } from "./navigation";

describe("dashboard navigation", () => {
  it("has the seven Lynewright areas in the approved order", () => {
    expect(PRIMARY_NAV.map((item) => item.label)).toEqual(["Overview", "Lines", "Runs", "Reviews", "Integrations", "Catalogue", "Settings"]);
  });
  it("keeps Operator and Application Logs inside Settings", () => {
    expect(SETTINGS_NAV.map((item) => [item.path, item.label])).toEqual([
      ["/settings", "AI providers"],
      ["/settings/operator", "Operator"],
      ["/settings/telegram", "Telegram"],
      ["/settings/logs", "Application logs"],
    ]);
  });
  it("adds Users as an admin-only Settings tab", () => {
    expect(ADMIN_SETTINGS_NAV).toEqual({ page: "users", path: "/settings/users", label: "Users" });
  });
});
