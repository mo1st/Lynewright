import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const projectId = "project-items";
const tenantId = "tenant-items";
const runId = "run-foreach-items";
const now = Math.floor(Date.now() / 1000);
const line = {
  id: "line-foreach", name: "Items walkthrough", project_id: projectId, enabled: true, updated_at: now,
  definition: { stages: [
    { id: "batch", kind: "for_each", label: "Process items" },
    { id: "shape", kind: "map", label: "Shape results" },
  ] },
};
const run = {
  id: runId, line_id: line.id, project_id: projectId, status: "done_with_errors", current_stage: "",
  context: { vars: {}, stages: {} }, created_at: now - 300, updated_at: now - 10,
  stages: [{ stage_id: "batch", status: "done_with_errors", attempts: 1, started_at: now - 300,
    ended_at: now - 10, duration_s: 290, output_ref: "", output: { total: 3, succeeded: 2, failed: 1, skipped: 0, results_ref: `${runId}:batch` }, progress: {}, error: "" }],
  timeline: [{ id: 1, type: "run.started", stage_id: "", detail: {}, actor_id: "demo", created_at: now - 300 }],
};
const items = {
  total: 3, succeeded: 2, failed: 1, skipped: 0, results_ref: `${runId}:batch`,
  items: [
    { item_index: 0, status: "succeeded", input: { label: "First item", value: "alpha" }, output: { result: "Completed" }, error: "", attempts: 1, started_at: now - 300, ended_at: now - 220 },
    { item_index: 1, status: "failed", input: { label: "Second item", value: "beta" }, output: null, error: "Temporary operation failure", attempts: 1, started_at: now - 210, ended_at: now - 100 },
    { item_index: 2, status: "succeeded", input: { label: "Third item", value: "gamma" }, output: { result: "Completed" }, error: "", attempts: 1, started_at: now - 90, ended_at: now - 10 },
  ],
};

test("captures the Items tab at desktop and mobile sizes in both themes", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(({ token, tenant, project }) => {
    localStorage.setItem("lynewright.session-token", token);
    localStorage.setItem("lynewright.active-tenant", tenant);
    localStorage.setItem("lynewright.active-project", project);
    localStorage.setItem(`lynewright.active-project:${tenant}`, project);
  }, { token: "screenshot-session", tenant: tenantId, project: projectId });

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const json = (payload: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
    if (url.pathname === "/api/auth/me") return json({
      id: 42, email: "viewer@example.test", role: "admin", is_active: true, active_tenant_id: tenantId,
      tenants: [{ id: tenantId, slug: "items", name: "Items", role: "admin", default_project_id: projectId }],
    });
    if (url.pathname === `/api/tenants/${tenantId}/projects`) return json([{ project_id: projectId, tenant_id: tenantId, name: "Demo project" }]);
    if (url.pathname === "/api/production-lines" && route.request().method() === "GET") return json({ items: [line] });
    if (url.pathname === "/api/production-lines/runs" && route.request().method() === "GET") return json({ items: [run] });
    if (url.pathname === `/api/production-lines/runs/${runId}`) return json(run);
    if (url.pathname === `/api/production-lines/runs/${runId}/stages/batch/items`) {
      const status = url.searchParams.get("status");
      return json(status && status !== "all" ? { ...items, items: items.items.filter((item) => item.status === status) } : items);
    }
    if (url.pathname === "/api/operator/approvals") return json([]);
    if (url.pathname === "/api/integration-management") return json({ items: [], package_statuses: [], install_policy: "catalogue-only" });
    if (url.pathname === "/api/catalogue") return json({ entries: [], status: { dev_key_trusted: false }, serial: 1 });
    return json({ items: [] });
  });

  const output = path.resolve("test-results/l1-foreach");
  fs.mkdirSync(output, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/runs/${runId}?tab=items`);
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Items", selected: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Items for Process items" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Items for Process items" }).getByRole("cell", { name: "Temporary operation failure" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry failed items" })).toBeVisible();

  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize({ width: 1440, height: 900 });
    if (theme === "light") {
      await page.locator(".ui-account-trigger").click();
      await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: "Light", exact: true }).click();
    } else {
      await page.locator(".ui-account-trigger").click();
      await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: "Dark", exact: true }).click();
    }
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.locator(".ui-account-trigger").click();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 375, height: 900 }]) {
      await page.setViewportSize(viewport);
      await page.locator("#items-status-filter").selectOption(viewport.width === 375 ? "failed" : "all");
      await expect(page.getByRole("tab", { name: "Items", selected: true })).toBeVisible();
      if (viewport.width === 375) {
        await expect(page.getByRole("list", { name: "Items for Process items" }).getByText("Temporary operation failure")).toBeVisible();
      } else {
        await expect(page.getByRole("table", { name: "Items for Process items" }).getByRole("cell", { name: "Temporary operation failure" })).toBeVisible();
      }
      const viewportState = await page.evaluate(() => ({ width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth }));
      expect(viewportState.scrollWidth).toBeLessThanOrEqual(viewportState.width + 1);
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `Items tab axe at ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `items-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: false, animations: "disabled" });
    }
  }
  expect(pageErrors).toEqual([]);
});
