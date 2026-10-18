export type SettingsPanel = "providers" | "operator" | "telegram" | "logs" | "users" | "credentials" | "media";
export type AreaPage = "overview" | "lines" | "runs" | "reviews" | "integrations" | "catalogue" | "settings" | "operator" | "telegram" | "logs" | "users" | "credentials" | "media" | "account" | "oauth" | "design";

export const PRIMARY_NAV: Array<{ page: AreaPage; path: string; label: string }> = [
  { page: "overview", path: "/overview", label: "Overview" },
  { page: "lines", path: "/lines", label: "Lines" },
  { page: "runs", path: "/runs", label: "Runs" },
  { page: "reviews", path: "/reviews", label: "Reviews" },
  { page: "integrations", path: "/integrations", label: "Integrations" },
  { page: "catalogue", path: "/catalogue", label: "Catalogue" },
  { page: "settings", path: "/settings", label: "Settings" },
];
export const SETTINGS_NAV: Array<{ page: AreaPage; path: string; label: string }> = [
  { page: "settings", path: "/settings", label: "AI providers" },
  { page: "operator", path: "/settings/operator", label: "Operator" },
  { page: "telegram", path: "/settings/telegram", label: "Telegram" },
  { page: "logs", path: "/settings/logs", label: "Application logs" },
];
export const ADMIN_SETTINGS_NAV: { page: AreaPage; path: string; label: string } = {
  page: "users", path: "/settings/users", label: "Users",
};
export const ADMIN_SETTINGS_EXTRA_NAV: Array<{ page: AreaPage; path: string; label: string }> = [
  { page: "credentials", path: "/settings/credentials", label: "Credentials" },
  { page: "media", path: "/settings/media", label: "Media" },
];
export function settingsPath(section: SettingsPanel): string {
  return section === "providers" ? "/settings" : `/settings/${section}`;
}

export const SETTINGS_ADVANCED_DRAWERS = [{ id: "provider-status", label: "Provider status", hint: "Connection diagnostics" }] as const;
