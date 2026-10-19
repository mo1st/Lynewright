import fs from "node:fs";
import path from "node:path";
import { test, expect } from "@playwright/test";

const user = {
  id: 1,
  email: "screenshot@example.test",
  role: "owner",
  is_active: true,
  active_tenant_id: "tenant-demo",
  tenants: [{ id: "tenant-demo", slug: "demo", name: "Demo", role: "owner", default_project_id: "project-demo" }],
};

test("captures the login and sidebar brands in light and dark at desktop and mobile widths", async ({ page }) => {
  const output = path.resolve(process.env.LYNEWRIGHT_LOGO_SCREENSHOT_DIR ?? "test-results/logo-mark");
  fs.mkdirSync(output, { recursive: true });

  for (const theme of ["light", "dark"] as const) {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await page.goto("/overview");
      await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
      await page.evaluate((selectedTheme) => { document.documentElement.dataset.theme = selectedTheme; }, theme);
      await page.screenshot({ path: path.join(output, `login-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
    }
  }

  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (!pathname.startsWith("/api/")) { await route.continue(); return; }
    let body: unknown = {};
    if (pathname.endsWith("/api/auth/me")) body = user;
    else if (pathname.endsWith("/api/tenants/tenant-demo/projects")) body = [{ project_id: "project-demo", tenant_id: "tenant-demo", name: "Demo project" }];
    else if (pathname === "/api/production-lines" || pathname === "/api/production-lines/runs") body = { items: [] };
    else if (pathname === "/api/operator/approvals") body = [];
    else if (pathname === "/api/integration-management") body = { items: [], package_statuses: [], install_policy: "confirm" };
    else if (pathname === "/api/catalogue") body = { status: { dev_key_trusted: false }, entries: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.evaluate(() => localStorage.setItem("lynewright.session-token", "screenshot-token"));

  for (const theme of ["light", "dark"] as const) {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await page.reload();
      await expect(page.locator("#main-content")).toBeVisible();
      const sidebar = page.locator("aside");
      await expect(sidebar.locator("svg").first()).toHaveAttribute("aria-hidden", "true");
      await expect(sidebar).toContainText("Lynewright");
      await page.evaluate((selectedTheme) => { document.documentElement.dataset.theme = selectedTheme; }, theme);
      await page.screenshot({ path: path.join(output, `sidebar-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled", fullPage: true });
    }
  }
});
