import fs from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const email = process.env.LYNEWRIGHT_SCREENSHOT_EMAIL ?? "";
const password = process.env.LYNEWRIGHT_SCREENSHOT_PASSWORD ?? "";
const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens/a1-assistants");

async function signIn(page: Page) {
  await page.goto("/overview");
  const emailField = page.getByLabel("Email");
  if (await emailField.isVisible().catch(() => false)) {
    await emailField.fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page.locator("#main-content")).toBeVisible();
}

async function setTheme(page: Page, theme: "light" | "dark") {
  await page.locator(".ui-account-trigger").click();
  const label = theme[0].toUpperCase() + theme.slice(1);
  await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: label, exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await page.locator(".ui-account-trigger").click();
}

test("Assistants settings render with live client data at desktop and mobile", async ({ page }) => {
  test.skip(!email || !password, "The screenshot runner supplies fresh bootstrap credentials and a temporary data directory.");
  fs.mkdirSync(output, { recursive: true });
  await signIn(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/assistants");
  await expect(page.getByRole("heading", { name: "Assistants", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy MCP connection URL" })).toBeVisible();
  await expect(page.getByText("HTTPS needed for web assistants")).toBeVisible();
  await expect(page.getByText(/127\.0\.0\.1:8000\/mcp/)).toBeVisible();

  await page.getByRole("button", { name: "Add assistant" }).click();
  const createDialog = page.getByRole("dialog", { name: "Add assistant" });
  await createDialog.getByLabel("Assistant name").fill("A1 screenshot assistant");
  await createDialog.getByRole("button", { name: "Create client" }).click();
  const tokenDialog = page.getByRole("dialog", { name: "Assistant connected" });
  await expect(tokenDialog.getByText("One-time access token")).toBeVisible();
  await tokenDialog.getByRole("button", { name: "Done" }).click();
  const clientRow = page.getByRole("row").filter({ hasText: "A1 screenshot assistant" });
  await expect(clientRow).toBeVisible();

  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize({ width: 1440, height: 900 });
    await setTheme(page, theme);
    for (const viewport of [{ width: 1440, height: 900 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await page.goto("/settings/assistants");
      await expect(page.getByRole("heading", { name: "Assistants", exact: true })).toBeVisible();
      await expect(page.getByRole("row").filter({ hasText: "A1 screenshot assistant" })).toBeVisible();
      const activeTab = page.getByRole("tablist", { name: "Settings sections" }).getByRole("tab", { name: "Assistants" });
      const tabVisibility = await activeTab.evaluate((element) => {
        const tablist = element.parentElement!;
        const active = element.getBoundingClientRect();
        const list = tablist.getBoundingClientRect();
        return { visible: active.left >= list.left && active.right <= list.right, scrollLeft: tablist.scrollLeft, active: { left: active.left, right: active.right }, list: { left: list.left, right: list.right, clientWidth: tablist.clientWidth, scrollWidth: tablist.scrollWidth } };
      });
      expect(tabVisibility.visible, `active Assistants tab stays visible at ${viewport.width}px: ${JSON.stringify(tabVisibility)}`).toBe(true);
      if (viewport.width === 375) expect(tabVisibility.scrollLeft, "mobile tab list scrolls to the active tab").toBeGreaterThan(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
      expect(overflow, `no page overflow at ${viewport.width}px in ${theme} theme`).toBe(false);
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical").map((violation) => violation.id), `axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({
        path: path.join(output, `settings-assistants-${viewport.width}x${viewport.height}-${theme}.png`),
        animations: "disabled",
      });
    }
  }
});
