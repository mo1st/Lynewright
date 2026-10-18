import fs from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Account and Users are exercised by the U4b live-user flow below, including their dialogs.
const routes = ["/overview", "/lines", "/runs", "/reviews", "/integrations", "/catalogue", "/settings", "/settings/operator", "/settings/telegram", "/settings/logs", "/design"];
const routeTitles: Record<string, string> = {
  "/overview": "Overview", "/lines": "Lines", "/runs": "Runs", "/reviews": "Reviews",
  "/integrations": "Integrations", "/catalogue": "Catalogue", "/settings": "AI providers",
  "/settings/operator": "Operator", "/settings/telegram": "Telegram", "/settings/logs": "Application logs", "/settings/users": "Users", "/account": "Account", "/design": "Design system",
};
function envFileValue(name: string): string {
  const candidates = [path.resolve("../.env"), path.resolve(".env")];
  for (const filename of candidates) {
    if (!fs.existsSync(filename)) continue;
    const line = fs.readFileSync(filename, "utf8").split(/\r?\n/).find((entry) => entry.startsWith(`${name}=`));
    if (line) return line.slice(name.length + 1).replace(/^['"]|['"]$/g, "");
  }
  return "";
}
const email = process.env.LYNEWRIGHT_SCREENSHOT_EMAIL ?? process.env.LYNEWRIGHT_BOOTSTRAP_EMAIL ?? envFileValue("LYNEWRIGHT_BOOTSTRAP_EMAIL");
const password = process.env.LYNEWRIGHT_SCREENSHOT_PASSWORD ?? process.env.LYNEWRIGHT_BOOTSTRAP_PASSWORD ?? envFileValue("LYNEWRIGHT_BOOTSTRAP_PASSWORD");

type LegacyCssCoverage = {
  session: Awaited<ReturnType<Page["context"]>["newCDPSession"]>;
  styleSheetIds: Set<string>;
};
const legacyCssCoverage = new WeakMap<Page, LegacyCssCoverage>();

test.beforeEach(async ({ page }) => {
  if (process.env.LYNEWRIGHT_CSS_COVERAGE !== "1") return;
  const session = await page.context().newCDPSession(page);
  const styleSheetIds = new Set<string>();
  session.on("CSS.styleSheetAdded", (event: { header: { styleSheetId: string; sourceURL: string } }) => {
    styleSheetIds.add(event.header.styleSheetId);
  });
  await session.send("DOM.enable");
  await session.send("CSS.enable");
  await session.send("CSS.startRuleUsageTracking");
  legacyCssCoverage.set(page, { session, styleSheetIds });
});

test.afterEach(async ({ page }) => {
  const measurement = legacyCssCoverage.get(page);
  const output = process.env.LYNEWRIGHT_CSS_COVERAGE_FILE;
  if (!measurement || !output) return;
  try {
    const result = await measurement.session.send("CSS.stopRuleUsageTracking") as {
      ruleUsage: Array<{ styleSheetId: string; startOffset: number; endOffset: number; used: boolean }>;
    };
    const legacyStyleSheetIds = new Set<string>();
    const legacyStyleSheetText = new Map<string, string>();
    for (const styleSheetId of measurement.styleSheetIds) {
      try {
        const content = await measurement.session.send("CSS.getStyleSheetText", { styleSheetId }) as { text: string };
        if (content.text.includes("legacyDashboardRoot")) {
          legacyStyleSheetIds.add(styleSheetId);
          legacyStyleSheetText.set(styleSheetId, content.text);
        }
      } catch { /* Browser-owned and cross-origin stylesheets are not the U0 wrapper. */ }
    }
    const rules = result.ruleUsage.filter((rule) => legacyStyleSheetIds.has(rule.styleSheetId));
    const totalRules = await page.evaluate(() => {
      const legacySheets = Array.from(document.styleSheets).filter((sheet) => {
        try { return String(sheet.ownerNode?.textContent ?? "").includes("legacyDashboardRoot"); }
        catch { return false; }
      });
      const countRules = (list: CSSRuleList): number => Array.from(list).reduce((count, rule) => {
        const declaration = "selectorText" in rule ? 1 : 0;
        const nested = "cssRules" in rule ? countRules((rule as CSSGroupingRule).cssRules) : 0;
        return count + declaration + nested;
      }, 0);
      return legacySheets.reduce((count, sheet) => {
        try { return count + countRules(sheet.cssRules); }
        catch { return count; }
      }, 0);
    });
    fs.appendFileSync(output, `${JSON.stringify({ totalRules, rules: rules.map(({ styleSheetId, startOffset, endOffset, used }) => ({
      startOffset, endOffset, used,
      cssText: legacyStyleSheetText.get(styleSheetId)?.slice(startOffset, endOffset) ?? "",
    })) })}\n`, "utf8");
  } finally {
    await measurement.session.detach();
    legacyCssCoverage.delete(page);
  }
});

async function signIn(page: Page) {
  await page.goto("/overview");
  const emailField = page.getByLabel("Email");
  if (await emailField.isVisible().catch(() => false)) {
    await emailField.fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page).toHaveURL(/\/overview$/);
}

async function setTheme(page: Page, theme: "light" | "dark") {
  const account = page.locator(".ui-account-trigger");
  await account.click();
  const themeLabel = theme[0].toUpperCase() + theme.slice(1);
  await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: themeLabel, exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await account.click();
}

async function dashboardApi<T>(page: Page, url: string, method = "GET", body?: unknown): Promise<T> {
  return page.evaluate(async ({ requestUrl, requestMethod, payload }) => {
    const token = localStorage.getItem("lynewright.session-token") ?? "";
    const tenant = localStorage.getItem("lynewright.active-tenant") ?? "";
    const project = localStorage.getItem("lynewright.active-project") ?? "";
    const response = await fetch(requestUrl, { method: requestMethod, headers: {
      Authorization: `Bearer ${token}`, "X-Tenant-ID": tenant, "X-Project-ID": project,
      ...(payload === undefined ? {} : { "Content-Type": "application/json" }),
    }, body: payload === undefined ? undefined : JSON.stringify(payload) });
    const text = await response.text();
    if (!response.ok) throw new Error(`${requestMethod} ${requestUrl}: HTTP ${response.status}: ${text.slice(0, 500)}`);
    return (text ? JSON.parse(text) : undefined) as T;
  }, { requestUrl: url, requestMethod: method, payload: body });
}

async function waitForRun(page: Page, runId: string, statuses: string[]) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    const run = await dashboardApi<{ id: string; status: string }>(page, `/api/production-lines/runs/${encodeURIComponent(runId)}`);
    if (statuses.includes(run.status)) return run;
    await page.waitForTimeout(250);
  }
  throw new Error(`Run ${runId} did not reach ${statuses.join(" or ")}.`);
}

async function seedU2Runs(page: Page) {
  const picker = page.locator("#project-picker");
  await picker.selectOption("lynewright-demo-media");
  await expect(picker).toHaveValue("lynewright-demo-media");
  const projectId = "lynewright-demo-media";
  const lines = await dashboardApi<{ items: Array<{ id: string; name: string }> }>(page, `/api/production-lines?project_id=${projectId}&include_shared=true`);
  const base = lines.items.find((line) => line.id === "demo-write-review-format-notify");
  const media = lines.items.find((line) => line.id === "demo-media-review-link-upload-notify");
  if (!base || !media) throw new Error("Both demo lines were not seeded by LYNEWRIGHT_DEMO=1.");
  const start = async (lineId: string, topic: string) => dashboardApi<{ id: string }>(page, `/api/production-lines/${encodeURIComponent(lineId)}/runs`, "POST", { project_id: projectId, vars: { topic } });
  const completed = await start(base.id, "U2 completed walkthrough");
  await waitForRun(page, completed.id, ["waiting"]);
  await dashboardApi(page, `/api/production-lines/runs/${encodeURIComponent(completed.id)}/continue`, "POST", { value: { approved: true, items: [{ id: "draft", decision: "approved", reason: "" }] } });
  await waitForRun(page, completed.id, ["done"]);
  const rejected = await start(base.id, "U2 rejected walkthrough");
  await waitForRun(page, rejected.id, ["waiting"]);
  await dashboardApi(page, `/api/production-lines/runs/${encodeURIComponent(rejected.id)}/continue`, "POST", { value: { approved: false, items: [{ id: "draft", decision: "refused", reason: "Reject screenshot seed" }] } });
  await waitForRun(page, rejected.id, ["rejected"]);
  const waiting = await start(base.id, "U2 waiting walkthrough");
  await waitForRun(page, waiting.id, ["waiting"]);
  const mediaWaiting = await start(media.id, "U2 media walkthrough");
  await waitForRun(page, mediaWaiting.id, ["waiting"]);
  return { lineId: base.id, mediaRunId: mediaWaiting.id };
}

async function assertLayout(page: Page, route: string) {
  const layout = await page.evaluate(() => {
    const clipped: string[] = [];
    const candidates = Array.from(document.querySelectorAll<HTMLElement>("button, input, select, textarea, [role=tab], img, pre, table"));
    for (const element of candidates) {
      if (!element.getClientRects().length) continue;
      const bounds = element.getBoundingClientRect();
      let parent = element.parentElement;
      while (parent) {
        const style = getComputedStyle(parent);
        if ([style.overflowX, style.overflowY].some((value) => value === "hidden" || value === "clip" || value === "auto" || value === "scroll")) break;
        parent = parent.parentElement;
      }
      if (!parent) continue;
      const style = getComputedStyle(parent);
      const scrollsHorizontally = ["auto", "scroll"].includes(style.overflowX) && parent.scrollWidth > parent.clientWidth + 1;
      if (scrollsHorizontally) continue;
      const parentBounds = parent.getBoundingClientRect();
      if (bounds.left < parentBounds.left - 2 || bounds.right > parentBounds.right + 2) {
        clipped.push(`${element.tagName.toLowerCase()}${element.getAttribute("aria-label") ? `[${element.getAttribute("aria-label")}]` : ""} escapes ${parent.className || parent.tagName}`);
      }
    }
    return { viewport: window.innerWidth, width: document.documentElement.scrollWidth, clipped: clipped.slice(0, 8) };
  });
  expect(layout.width, `no page-level horizontal overflow at ${route} (${layout.viewport}px)`).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.clipped, `controls stay inside their nearest clipping/scroll container at ${route}: ${layout.clipped.join(", ")}`).toEqual([]);
  const header = page.locator("main h1").first();
  const titleBox = await header.boundingBox();
  const action = page.locator("main header").first().locator("button").first();
  if (await action.count() && await action.isVisible().catch(() => false) && titleBox) {
    const actionBox = await action.boundingBox();
    if (actionBox && layout.viewport > 600) expect(titleBox.x + titleBox.width <= actionBox.x + 1 || titleBox.y + titleBox.height <= actionBox.y + 1, `header title and actions do not overlap at ${route}`).toBe(true);
  }
}

test("captures every current page and checks the design gallery in light and dark", async ({ page }) => {
  test.setTimeout(720_000);
  test.skip(!email || !password, "Set LYNEWRIGHT_BOOTSTRAP_EMAIL and LYNEWRIGHT_BOOTSTRAP_PASSWORD (or configure .env) to capture authenticated pages.");
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedApiResponses: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("response", (response) => { if (response.url().includes("/api/") && response.status() >= 400) failedApiResponses.push(`${response.status()} ${response.url()}`); });
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  fs.mkdirSync(output, { recursive: true });
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  // Capture the unauthenticated screen at the same two sizes and themes.
  for (const theme of ["light", "dark"] as const) {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/overview");
      await page.evaluate((selectedTheme) => { document.documentElement.dataset.theme = selectedTheme; }, theme);
      await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
      await expect(page).toHaveTitle("Sign in · Lynewright");
      const loginAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(loginAxe.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical").map((violation) => violation.id), `login axe serious/critical violations at ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `login-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
    }
  }
  await signIn(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page).toHaveTitle("Overview · Lynewright");
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(viewports[0]);
    await setTheme(page, theme);
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const route of routes) {
        await page.goto(route);
        await expect(page).toHaveTitle(`${routeTitles[route]} · Lynewright`);
        await expect(page.locator("main h1").first()).toHaveText(route.startsWith("/settings") ? "Settings" : routeTitles[route]);
        await expect(page.locator("main").first()).toBeVisible();
        if (["/lines", "/runs", "/reviews"].includes(route)) await expect(page.getByText("Loading production lines and runs…", { exact: true })).toHaveCount(0);
        if (route === "/integrations") await expect(page.getByText("Loading integrations…", { exact: true })).toHaveCount(0);
        if (route === "/catalogue") await expect(page.getByText("Loading catalogue…", { exact: true })).toHaveCount(0);
        await page.waitForTimeout(200);
        if (viewport.width === 1440 && route !== "/design") {
          const nav = page.getByRole("navigation", { name: "Primary navigation" });
          const expectedNav = ["Overview", "Lines", "Runs", "Reviews", "Integrations", "Catalogue", "Settings"];
          const labels = await nav.locator(":scope > button").evaluateAll((items, expected) => items.map((item) => expected.find((label) => (item.textContent ?? "").startsWith(label)) ?? item.textContent?.trim()), expectedNav);
          expect(labels).toEqual(expectedNav);
          const activeName = route.startsWith("/settings") ? "Settings" : routeTitles[route];
          expect((await nav.locator('[aria-current="page"]').textContent())?.trim()).toContain(activeName);
          const shell = await page.locator("aside[aria-label='Primary navigation']").boundingBox();
          const content = await page.locator("#main-content").boundingBox();
          expect(shell && content && shell.x + shell.width <= content.x + 1, `sidebar and content do not overlap at ${route}`).toBe(true);
        }
        if (["/lines", "/runs", "/reviews"].includes(route)) {
          await expect(page.locator(".production-metrics")).toHaveCount(0);
          await expect(page.getByRole("tablist", { name: "Production views" })).toHaveCount(0);
        }
        if (route === "/overview") {
          await expect(page.locator("#project-picker")).not.toHaveValue("");
          const projectName = (await page.locator("#project-picker option:checked").textContent())?.trim() ?? "";
          const metricValues = page.locator('[aria-label^="Production summary for"] strong');
          await expect(metricValues).toHaveCount(4);
          await expect(metricValues.first()).not.toHaveText("…");
          await expect(page.locator('[aria-label^="Production summary for"]')).toContainText(projectName);
          await expect(page.getByRole("heading", { name: "Needs attention", exact: true })).toBeVisible();
          if (theme === "light" && viewport.width === 1440) {
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            const serious = results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical");
            expect(serious.map((violation) => violation.id), `overview axe serious/critical violations: ${JSON.stringify(serious.map((violation) => ({ id: violation.id, nodes: violation.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) })))}`).toEqual([]);
          }
        }
        if (route.startsWith("/settings")) {
          const tabs = page.getByRole("tablist", { name: "Settings sections" });
          await expect(tabs).toBeVisible();
          await expect(tabs.getByRole("tab", { selected: true })).toHaveText(routeTitles[route]);
          await expect(page.getByLabel("Settings section", { exact: true })).toHaveCount(0);
          const tabMetrics = await tabs.evaluate((element) => ({ height: element.clientHeight, scrollHeight: element.scrollHeight }));
          expect(tabMetrics.scrollHeight).toBeLessThanOrEqual(tabMetrics.height + 2);
          if (route === "/settings" && theme === "light" && viewport.width === 1440) {
            await expect(page.locator(".settings-provider-section")).toBeVisible();
            await expect(page.locator(".settings-diagnostics")).toBeVisible();
            const providerWidths = await page.evaluate(() => {
              const providers = document.querySelector<HTMLElement>(".settings-provider-section");
              const diagnostics = document.querySelector<HTMLElement>(".settings-diagnostics");
              return providers && diagnostics ? [providers.getBoundingClientRect().width, diagnostics.getBoundingClientRect().width] : [];
            });
            expect(providerWidths.length).toBe(2);
            expect(Math.abs(providerWidths[0] - providerWidths[1])).toBeLessThanOrEqual(1);
            const fallbackSubtitle = page.getByText("Fixed last route · no external service", { exact: true });
            await expect(fallbackSubtitle).toBeVisible();
            await expect(fallbackSubtitle).toHaveCSS("display", "block");
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            expect(results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical").map((violation) => violation.id), "settings axe serious/critical violations").toEqual([]);
          }
        }
        if (viewport.width === 375) {
          const mobileNav = page.getByRole("navigation", { name: "Mobile navigation" });
          await expect(mobileNav).toBeVisible();
          const expectedMobileNav = ["Overview", "Lines", "Reviews", "Integrations", "More"];
          const mobileLabels = await mobileNav.locator(":scope > button").evaluateAll((items, expected) => items.map((item) => expected.find((label) => (item.textContent ?? "").startsWith(label)) ?? item.textContent?.trim()), expectedMobileNav);
          expect(mobileLabels).toEqual(["Overview", "Lines", "Reviews", "Integrations", "More"]);
          if (route === "/overview") {
            await expect(mobileNav.getByRole("button", { name: "Overview" })).toHaveAttribute("aria-current", "page");
            await expect(mobileNav.getByRole("button", { name: "Reviews" })).not.toHaveAttribute("aria-current", "page");
          }
          if (route === "/reviews") {
            await expect(mobileNav.getByRole("button", { name: "Reviews" })).toHaveAttribute("aria-current", "page");
            await expect(mobileNav.getByRole("button", { name: "Overview" })).not.toHaveAttribute("aria-current", "page");
          }
          if (route === "/overview" && theme === "light") {
            await mobileNav.getByRole("button", { name: "More" }).click();
            const sheet = page.getByRole("dialog", { name: "More" });
            await expect(sheet).toBeVisible();
            for (const label of ["Runs", "Catalogue", "Settings"]) await expect(sheet.getByRole("button", { name: label })).toBeVisible();
            await expect(sheet.getByLabel("Project")).toBeVisible();
            const account = sheet.getByRole("button", { name: /@example\.test$/ });
            await expect(account).toBeVisible();
            await account.click();
            await expect(sheet.getByRole("group", { name: "Theme" })).toBeVisible();
            await expect(sheet.getByRole("button", { name: "Sign out" })).toBeVisible();
            await page.screenshot({ path: path.join(output, "more-sheet-375x812-light.png"), animations: "disabled" });
            await page.keyboard.press("Escape");
            await expect(sheet).toHaveCount(0);
          }
        }
        if (route === "/design") {
          await expect(page.getByRole("heading", { name: "Design system", exact: true })).toBeVisible();
          for (const themeName of ["light", "dark"] as const) {
            const themePanel = page.locator(`section[data-theme="${themeName}"][aria-label="${themeName} component theme"]`);
            for (const variant of ["primary", "secondary", "ghost", "danger"] as const) {
              await expect(page.getByTestId(`${themeName}-button-${variant}`)).toBeVisible();
            }
            const colors = await themePanel.evaluate((panel) => {
              const get = (selector: string) => {
                const element = panel.querySelector<HTMLElement>(selector);
                if (!element) throw new Error(`Missing design gallery selector ${selector}`);
                const style = getComputedStyle(element);
                return { background: style.backgroundColor, border: style.borderTopColor, color: style.color };
              };
              const probe = document.createElement("span");
              probe.style.color = "var(--color-accent)";
              probe.style.backgroundColor = "var(--color-surface-raised)";
              panel.append(probe);
              const accent = getComputedStyle(probe).color;
              const raisedSurface = getComputedStyle(probe).backgroundColor;
              probe.remove();
              const tab = panel.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
              const nav = panel.querySelector<HTMLElement>(".design-active-nav");
              const navIndicator = nav ? getComputedStyle(nav, "::before").backgroundColor : "";
              const tabStyles = tab ? getComputedStyle(tab) : null;
              const smallButton = panel.querySelector<HTMLElement>('[data-testid$="-button-small"]');
              const secondaryButton = panel.querySelector<HTMLElement>('[data-button-variant="secondary"]');
              const longCell = panel.querySelector<HTMLElement>("[data-table-cell-text]");
              const actionCell = panel.querySelector<HTMLElement>("[data-table-actions]:has(button)");
              const actionButton = actionCell?.querySelector<HTMLElement>("button");
              const tableWrap = panel.querySelector<HTMLElement>("[data-table-wrap]");
              if (!smallButton || !secondaryButton || !longCell || !actionCell || !actionButton || !tableWrap) throw new Error("Missing button or table example");
              return {
                accent,
                raisedSurface,
                primary: get('[data-button-variant="primary"]'),
                secondary: get('[data-button-variant="secondary"]'),
                ghost: get('[data-button-variant="ghost"]'),
                danger: get('[data-button-variant="danger"]'),
                dangerConfirm: get('[data-button-variant="danger"].ui-button--danger-confirm'),
                small: get('[data-testid$="-button-small"]'),
                smallHeight: Number.parseFloat(getComputedStyle(smallButton).height),
                secondaryHeight: Number.parseFloat(getComputedStyle(secondaryButton).height),
                primaryCount: panel.querySelectorAll('[data-button-variant="primary"]').length,
                nonPrimaryAccent: Array.from(panel.querySelectorAll<HTMLElement>('button[data-button-variant]:not([data-button-variant="primary"])')).some((button) => {
                  const style = getComputedStyle(button);
                  return style.backgroundColor === accent || style.borderTopColor === accent;
                }),
                viewportWidth: window.innerWidth,
                longCellTitle: longCell.parentElement?.getAttribute("title") ?? "",
                longCellClipped: longCell.scrollWidth > longCell.clientWidth,
                actionCellWidth: actionCell.clientWidth,
                actionButtonWidth: actionButton.clientWidth,
                actionAlignment: getComputedStyle(actionCell).textAlign,
                tableWrapScrolls: tableWrap.scrollWidth > tableWrap.clientWidth,
                navIndicator,
                nav: nav ? getComputedStyle(nav).backgroundColor : "",
                tabIndicator: tabStyles?.borderBottomColor ?? "",
                tabText: tabStyles?.color ?? "",
              };
            });
            expect(colors.primary.background, `${themeName} primary uses accent`).toBe(colors.accent);
            const variantSurfaces = [colors.primary, colors.secondary, colors.ghost, colors.danger].map((variant) => `${variant.background}/${variant.border}`);
            expect(new Set(variantSurfaces).size, `${themeName} button background/border variants differ`).toBe(4);
            for (const variant of [colors.secondary, colors.ghost, colors.danger, colors.dangerConfirm]) {
              expect(variant.background).not.toBe(colors.accent);
              expect(variant.border).not.toBe(colors.accent);
            }
            expect(colors.primaryCount, `${themeName} has one accent primary`).toBe(1);
            expect(colors.nonPrimaryAccent, `${themeName} keeps accent off supporting button variants`).toBe(false);
            expect(colors.small.background).toBe(colors.secondary.background);
            expect(colors.small.border).toBe(colors.secondary.border);
            expect(colors.smallHeight).toBeLessThan(colors.secondaryHeight);
            expect(colors.longCellTitle).toBe("integration.example.generate_final_review_image_with_context_and_metadata");
            expect(colors.longCellClipped).toBe(true);
            expect(colors.actionCellWidth).toBeGreaterThanOrEqual(colors.actionButtonWidth);
            expect(colors.actionAlignment).toBe("end");
            if (colors.viewportWidth <= 600) expect(colors.tableWrapScrolls, `${themeName} narrow table scrolls inside its card`).toBe(true);
            expect(colors.navIndicator).toBe(colors.accent);
            expect(colors.nav).toBe(colors.raisedSurface);
            expect(colors.tabIndicator).toBe(colors.accent);
            expect(colors.tabText).not.toBe(colors.accent);
          }
          if (viewport.width === 1440) {
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            expect(results.violations.map((violation) => ({ id: violation.id, impact: violation.impact, nodes: violation.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) })), `${theme} /design axe violations`).toEqual([]);
          }
        }
        if (route === "/integrations") await expect(page.getByText("Loading integrations…", { exact: true })).toHaveCount(0);
        if (route === "/integrations") await expect(page.getByRole("tab", { name: "Catalogue" })).toHaveCount(0);
        if (route === "/catalogue") {
          await expect(page.getByRole("heading", { name: "Catalogue", exact: true })).toBeVisible();
          if (theme === "light" && viewport.width === 1440) {
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            const serious = results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical");
            expect(serious.map((violation) => ({ id: violation.id, nodes: violation.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) })), "catalogue axe serious/critical violations").toEqual([]);
          }
        }
        const axeResults = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        const seriousAxe = axeResults.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical");
        expect(seriousAxe.map((violation) => ({ id: violation.id, targets: violation.nodes.map((node) => node.target) })), `${route} axe serious/critical at ${viewport.width}px ${theme}`).toEqual([]);
        if (route !== "/design") {
          const duplicateHeadings = await page.locator("main h1, main h2, main h3, main h4").evaluateAll((nodes) => {
            const visible = nodes.filter((node) => node.getClientRects().length > 0).map((node) => (node.textContent ?? "").replace(/\s+/g, " ").trim()).filter(Boolean);
            return [...new Set(visible.filter((label, index) => visible.indexOf(label) !== index))];
          });
          expect(duplicateHeadings, `no repeated visible headings at ${route}`).toEqual([]);
        }
        const visibleDialog = page.locator('[role="dialog"][aria-modal="true"]:visible').first();
        if (await visibleDialog.count()) {
          const bounds = await visibleDialog.boundingBox();
          expect(bounds && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, `dialog fits ${viewport.width}x${viewport.height}`).toBe(true);
        }
        const wrappedTabs = await page.locator('[role="tablist"]').evaluateAll((tabs) => tabs.some((tab) => tab.scrollHeight > tab.clientHeight + 2));
        expect(wrappedTabs, `tabs stay in one line at ${route}`).toBe(false);
        const emptyHeaders = await page.locator(".panel-header, .section-heading").evaluateAll((headers) => headers.filter((header) => !(header.textContent ?? "").trim() && !header.querySelector("button, a, input, select")).length);
        expect(emptyHeaders, `no empty card headers at ${route}`).toBe(0);
        const file = path.join(output, `${route.slice(1).replaceAll("/", "-") || "home"}-${viewport.width}x${viewport.height}-${theme}.png`);
        await page.screenshot({ path: file, fullPage: route === "/design", animations: "disabled" });
        await assertLayout(page, route);
      }
    }
  }
  await page.setViewportSize(viewports[0]);
  await page.goto("/overview");
  await page.waitForLoadState("networkidle", { timeout: 2000 }).catch(() => undefined);
  const dismissWarning = page.getByRole("button", { name: "Dismiss message" });
  if (await dismissWarning.isVisible().catch(() => false)) {
    await dismissWarning.click();
    await expect(page.getByText(/Development catalogue key is trusted|Outside integrations can be installed/)).toHaveCount(0);
    await page.goto("/lines");
    await expect(page.getByText(/Development catalogue key is trusted|Outside integrations can be installed/)).toHaveCount(0);
  }
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
});

test("account trigger is accessible and stays inside its shell at every breakpoint", async ({ page }) => {
  test.skip(!email || !password, "The U4 screenshot runner supplies a fresh random bootstrap admin account.");
  await signIn(page);
  for (const width of [1440, 1024, 768, 375]) {
    await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
    await page.goto("/overview");
    let container: ReturnType<typeof page.locator>;
    if (width <= 768) {
      await page.getByRole("button", { name: "More" }).click();
      container = page.getByRole("dialog", { name: "More" });
    } else {
      container = page.locator("aside[aria-label='Primary navigation']");
    }
    const trigger = container.locator(".ui-account-trigger");
    await expect(trigger, `account trigger is visible at ${width}px`).toBeVisible();
    await expect(trigger, `account trigger has its visible email as the accessible name at ${width}px`).toHaveAccessibleName(new RegExp(email.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    const dimensions = await trigger.evaluate((element) => {
      const parent = element.closest("aside[aria-label='Primary navigation'], [role='dialog']");
      if (!parent) throw new Error("Account trigger is not inside the sidebar or More sheet.");
      const rect = element.getBoundingClientRect();
      const box = parent.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, parentLeft: box.left, parentRight: box.right, parentTop: box.top, parentBottom: box.bottom,
        title: element.querySelector<HTMLElement>("[title]")?.title ?? "", isButton: element.tagName === "BUTTON" };
    });
    expect(dimensions.isButton).toBe(true);
    expect(dimensions.title).toBe(email);
    expect(dimensions.left).toBeGreaterThanOrEqual(dimensions.parentLeft - 1);
    expect(dimensions.right).toBeLessThanOrEqual(dimensions.parentRight + 1);
    expect(dimensions.top).toBeGreaterThanOrEqual(dimensions.parentTop - 1);
    expect(dimensions.bottom).toBeLessThanOrEqual(dimensions.parentBottom + 1);
  }
});

test("U4b account and user management use live users at desktop and mobile", async ({ page }) => {
  test.setTimeout(180_000);
  test.skip(!email || !password, "The U4 screenshot runner supplies a fresh random bootstrap admin account.");
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  fs.mkdirSync(output, { recursive: true });
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedApiResponses: string[] = [];
  let expectedSignInRejection = false;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error" && !message.text().includes("401 (Unauthorized)")) consoleErrors.push(message.text()); });
  page.on("response", (response) => { const expectedAuthFailure = expectedSignInRejection && response.status() === 401 && new URL(response.url()).pathname.startsWith("/api/auth/"); if (response.url().includes("/api/") && response.status() >= 400 && !expectedAuthFailure) failedApiResponses.push(`${response.status()} ${response.url()}`); });
  await signIn(page);

  const runId = `${Date.now()}`;
  const firstUser = `u4b-viewer-${runId}@example.test`;
  const secondUser = `u4b-editor-${runId}@example.test`;
  const createThroughUi = async (userEmail: string, role: "viewer" | "editor") => {
    await page.goto("/settings/users");
    await page.getByRole("button", { name: "Add user" }).click();
    const dialog = page.getByRole("dialog", { name: "Add user" });
    await dialog.getByLabel("Email").fill(userEmail);
    await dialog.getByLabel("Role").selectOption(role);
    await dialog.getByLabel("Initial password").fill("Initial-user-pass-123!");
    await dialog.getByRole("button", { name: "Add user", exact: true }).click();
    await expect(page.getByRole("row").filter({ hasText: userEmail })).toBeVisible();
  };
  await createThroughUi(firstUser, "viewer");
  await createThroughUi(secondUser, "editor");

  const viewports = [{ width: 1440, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(viewports[0]);
    await setTheme(page, theme);
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/account");
      await expect(page.getByRole("heading", { name: "Account", exact: true })).toBeVisible();
      let axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `account axe ${viewport.width}px ${theme}`).toEqual([]);
      await assertLayout(page, "/account");
      await page.screenshot({ path: path.join(output, `u4b-account-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });

      await page.goto("/settings/users");
      await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
      const adminRow = page.getByRole("row").filter({ hasText: email });
      await expect(adminRow.getByText("You", { exact: true })).toBeVisible();
      const disableAdmin = adminRow.getByRole("button", { name: /Disable .* unavailable/ });
      const roleAdmin = adminRow.getByRole("button", { name: /Change role.*unavailable/ });
      await expect(disableAdmin).toBeDisabled();
      await expect(roleAdmin).toBeDisabled();
      await expect(disableAdmin).toHaveAttribute("title", /Add another active admin/);
      axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `users table axe ${viewport.width}px ${theme}`).toEqual([]);
      await assertLayout(page, "/settings/users");
      await page.screenshot({ path: path.join(output, `u4b-users-table-last-admin-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });

      await page.getByRole("button", { name: "Add user" }).click();
      const addDialog = page.getByRole("dialog", { name: "Add user" });
      await expect(addDialog).toBeVisible();
      let box = await addDialog.boundingBox();
      expect(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1, `add-user dialog fits ${viewport.width}px`).toBe(true);
      axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `add-user dialog axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `u4b-users-add-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      await addDialog.getByRole("button", { name: "Cancel" }).click();

      const firstRow = page.getByRole("row").filter({ hasText: firstUser });
      await firstRow.getByRole("button", { name: "Reset password" }).click();
      const resetDialog = page.getByRole("dialog", { name: `Reset password for ${firstUser}` });
      await expect(resetDialog.getByText("One-time password")).toBeVisible();
      await resetDialog.getByRole("button", { name: "Reset password" }).click();
      await expect(resetDialog.getByRole("button", { name: "Done" })).toBeVisible();
      expect((await resetDialog.locator("code").innerText()).length).toBeGreaterThanOrEqual(16);
      box = await resetDialog.boundingBox();
      expect(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1, `reset-password dialog fits ${viewport.width}px`).toBe(true);
      axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `reset-password dialog axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `u4b-users-reset-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      await resetDialog.getByRole("button", { name: "Done" }).click();

      const deletableRow = page.getByRole("row").filter({ hasText: secondUser });
      await deletableRow.getByRole("button", { name: `Delete ${secondUser}` }).click();
      const deleteDialog = page.getByRole("dialog", { name: `Delete ${secondUser}?` });
      await expect(deleteDialog).toBeVisible();
      const deleteButton = deleteDialog.getByRole("button", { name: "Delete user" });
      await expect(deleteButton).toBeDisabled();
      await deleteDialog.getByLabel("Email to confirm").fill(secondUser);
      await expect(deleteButton).toBeEnabled();
      box = await deleteDialog.boundingBox();
      expect(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1, `delete-user dialog fits ${viewport.width}px`).toBe(true);
      axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `delete-user dialog axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `u5-users-delete-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      await deleteDialog.getByRole("button", { name: "Cancel" }).click();
    }
  }
  await page.setViewportSize(viewports[0]);
  await page.goto("/settings/users");
  const secondRow = page.getByRole("row").filter({ hasText: secondUser });
  await secondRow.getByRole("button", { name: `Disable ${secondUser}` }).click();
  await page.getByRole("alertdialog", { name: `Disable ${secondUser}?` }).getByRole("button", { name: "Disable account" }).click();
  expectedSignInRejection = true;
  await page.locator("aside[aria-label='Primary navigation'] .ui-account-trigger").click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Email").fill(secondUser);
  await page.getByLabel("Password").fill("Initial-user-pass-123!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("This account is disabled. Ask an admin.");
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
});

test("U2 walkthrough captures production areas with seeded real runs in both themes and sizes", async ({ page }) => {
  test.setTimeout(360_000);
  test.skip(!email || !password, "The U2 runner script supplies a fresh random bootstrap admin account.");
  const pageErrors: string[] = []; const consoleErrors: string[] = []; const failedApiResponses: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("response", (response) => { if (response.url().includes("/api/") && response.status() >= 400) failedApiResponses.push(`${response.status()} ${response.url()}`); });
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens"); fs.mkdirSync(output, { recursive: true });
  await signIn(page);
  const seeded = await seedU2Runs(page);
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(viewports[0]); await setTheme(page, theme);
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const [key, route] of [
        ["overview-items", "/overview"], ["lines", "/lines"], ["line-detail", `/lines/${encodeURIComponent(seeded.lineId)}`],
        ["runs-filtered", "/runs?status=done"], ["run-detail-media", `/runs/${encodeURIComponent(seeded.mediaRunId)}`], ["reviews", "/reviews"],
      ] as const) {
        await page.goto(route); await expect(page.locator("#main-content")).toBeVisible(); await page.waitForTimeout(350); await page.evaluate(() => window.scrollTo(0, 0));
        await assertLayout(page, route);
        if (key === "overview-items") {
          await expect(page.getByRole("heading", { name: "Needs attention" })).toBeVisible(); await expect(page.getByRole("heading", { name: "Recent runs" })).toBeVisible();
          expect(await page.getByRole("heading", { name: "Nothing needs attention" }).count()).toBe(0);
        }
        if (key === "line-detail") { await expect(page.getByRole("heading", { name: /Demo: write/ })).toBeVisible(); await expect(page.getByRole("tab", { name: "Runs" })).toBeVisible(); expect(await page.getByRole("tab", { name: "Trends" }).count()).toBe(0); }
        if (key === "runs-filtered") await expect(page.getByRole("combobox", { name: "Status" })).toHaveValue("done");
        if (key === "run-detail-media") {
          await expect(page.getByRole("heading", { name: /Demo: image/ })).toBeVisible();
          const imageToggle = page.getByRole("button", { name: /review/ }).first(); if (await imageToggle.getAttribute("aria-expanded") !== "true") await imageToggle.click();
          const outputImage = page.locator("main img").first(); await expect(outputImage).toBeVisible(); await expect.poll(() => outputImage.evaluate((image) => image.complete && image.naturalWidth > 0)).toBe(true);
          const stageCard = page.locator("[data-stage-card]").filter({ has: page.locator("[data-stage-expanded-content]") }).first();
          const expandedContent = stageCard.locator("[data-stage-expanded-content]");
          const cardBox = await stageCard.boundingBox(); const contentBox = await expandedContent.boundingBox();
          expect(cardBox && contentBox && contentBox.width >= cardBox.width * 0.8, `expanded stage output should use at least 80% of its card at ${viewport.width}px`).toBe(true);
        }
        if (key === "reviews") {
          const mediaQueueItem = page.getByRole("button", { name: /Demo: image/ }).first(); await mediaQueueItem.click();
          await expect(page.getByRole("heading", { name: /Demo: image/ })).toBeVisible(); const focusImage = page.locator('section[aria-label="Focused review"] img').first(); await expect(focusImage).toBeVisible(); await expect.poll(() => focusImage.evaluate((image) => image.complete && image.naturalWidth > 0)).toBe(true);
          if (viewport.width === 1440) {
            const actions = page.locator('section[aria-label="Focused review"] [data-review-actions]'); const actionBox = await actions.boundingBox();
            expect(actionBox && actionBox.y + actionBox.height <= viewport.height, "review actions stay visible at desktop size").toBe(true);
            await expect(page.getByRole("button", { name: "Approve" })).toBeVisible(); await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
          }
          if (theme === "light" && viewport.width === 1440) {
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            const serious = results.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
            expect(serious.map((item) => ({ id: item.id, nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) })), "reviews axe serious/critical violations").toEqual([]);
          }
        }
        const areaAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(areaAxe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `${key} axe serious/critical at ${viewport.width}px ${theme}`).toEqual([]);
        if (theme === "light" && viewport.width === 1440 && ["lines", "runs-filtered"].includes(key)) {
          const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
          expect(results.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id)).toEqual([]);
        }
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: path.join(output, `${key}-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: false, animations: "disabled" });
      }
      if (viewport.width === 375) {
        await page.goto("/reviews"); const mediaQueueItem = page.getByRole("button", { name: /Demo: image/ }).first(); await mediaQueueItem.click();
        await expect(page.getByRole("button", { name: "Approve" })).toBeVisible(); await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
        const mobileFocusImage = page.locator('section[aria-label="Focused review"] img').first(); await expect(mobileFocusImage).toBeVisible(); await expect.poll(() => mobileFocusImage.evaluate((image) => image.complete && image.naturalWidth > 0)).toBe(true);
        const reviewAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(reviewAxe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `mobile focused review axe ${theme}`).toEqual([]);
        await page.screenshot({ path: path.join(output, `review-item-mobile-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: false, animations: "disabled" });
      }
    }
  }
  expect(pageErrors, "browser page errors").toEqual([]); expect(failedApiResponses, "failed API responses").toEqual([]); expect(consoleErrors, "browser console errors").toEqual([]);
  console.log(`U2 screenshots: ${output}`);
});

test("settings tabs follow routes and browser history, with one-line mobile scrolling", async ({ page }) => {
  test.skip(!email || !password, "The screenshot runner supplies fresh random admin credentials for this run.");
  await signIn(page);
  await page.goto("/settings");
  const tabs = page.getByRole("tablist", { name: "Settings sections" });
  await expect(tabs.getByRole("tab", { name: "AI providers" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Telegram" }).click();
  await expect(page).toHaveURL(/\/settings\/telegram$/);
  await expect(tabs.getByRole("tab", { name: "Telegram" })).toHaveAttribute("aria-selected", "true");
  await page.goBack();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(tabs.getByRole("tab", { name: "AI providers" })).toHaveAttribute("aria-selected", "true");
  await page.setViewportSize({ width: 375, height: 812 });
  const dimensions = await tabs.evaluate((element) => ({ height: element.clientHeight, scrollHeight: element.scrollHeight, scrollWidth: element.scrollWidth, width: element.clientWidth }));
  expect(dimensions.scrollHeight).toBeLessThanOrEqual(dimensions.height + 2);
  expect(dimensions.scrollWidth).toBeGreaterThanOrEqual(dimensions.width);
  await expect(page.getByLabel("Settings section", { exact: true })).toHaveCount(0);
});

test("captures the catalogue trust, filter, and install states in both themes and widths", async ({ page }) => {
  test.setTimeout(240_000);
  test.skip(!email || !password, "The screenshot runner supplies fresh random admin credentials for this run.");
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  fs.mkdirSync(output, { recursive: true });
  await signIn(page);
  const sizes = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(sizes[0]);
    await setTheme(page, theme);
    for (const viewport of sizes) {
      await page.setViewportSize(viewport);
      await page.goto("/catalogue");
      await expect(page.getByText("Development key", { exact: true })).toBeVisible();
      await expect(page.locator("article")).toHaveCount(3);
      await page.screenshot({ path: path.join(output, `u1b-catalogue-populated-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });

      await page.getByLabel("Search catalogue").fill("no-such-integration-784");
      await expect(page.getByRole("heading", { name: "No results" })).toBeVisible();
      await page.screenshot({ path: path.join(output, `u1b-catalogue-filtered-empty-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });

      await page.getByLabel("Search catalogue").fill("");
      const fileCard = page.locator("article").filter({ hasText: "Setup required" }).first();
      await fileCard.getByRole("button", { name: "Install", exact: true }).click();
      await expect(page.getByLabel("Internal endpoint reference")).toBeVisible();
      await page.screenshot({ path: path.join(output, `u1b-catalogue-install-file-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });
      await page.getByRole("button", { name: "Cancel", exact: true }).click();

      const packageCard = page.locator("article").filter({ hasText: "Native Python Example" }).first();
      const packageInstallButton = packageCard.getByRole("button", { name: "Install", exact: true });
      if (await packageInstallButton.isVisible().catch(() => false)) {
        await packageInstallButton.click({ timeout: 5_000 }).catch(() => undefined);
      }
      if (await page.getByText("Runs in its own container").isVisible().catch(() => false)) {
        await page.screenshot({ path: path.join(output, `u1b-catalogue-install-package-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
      } else {
        await expect(packageCard).toContainText("Installed");
        await page.screenshot({ path: path.join(output, `u1b-catalogue-package-installed-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });
      }

      const catalogueRouteMatcher = (url: URL) => url.pathname === "/api/catalogue";
      await page.route(catalogueRouteMatcher, async (route) => {
        if (route.request().method() !== "GET") { await route.continue(); return; }
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          status: { url: "", status: "untrusted", trusted_key_ids: [], dev_key_trusted: false, dev_key_warning: false, dev_key_warning_message: null, verified_key_id: null, serial: null, generated_at: null, fetched_at: null, last_error: "no catalogue keys configured" },
          entries: [],
        }) });
      });
      await page.goto("/catalogue");
      await expect(page.getByText("Loading catalogue…", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Untrusted", { exact: true })).toBeVisible();
      const untrustedAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(untrustedAxe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `untrusted catalogue axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `u1b-catalogue-untrusted-${viewport.width}x${viewport.height}-${theme}.png`), fullPage: true, animations: "disabled" });
      await page.unroute(catalogueRouteMatcher);
    }
  }
});

test("login error stays below the form without moving the card and meets serious/critical axe checks", async ({ page }) => {
  await page.route("**/api/auth/login", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Email or password is incorrect." }) }));
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  fs.mkdirSync(output, { recursive: true });
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/overview");
      await page.evaluate((selectedTheme) => { document.documentElement.dataset.theme = selectedTheme; }, theme);
      await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
      const form = page.locator("form");
      const before = await form.evaluate((element) => element.getBoundingClientRect().height);
      await page.getByLabel("Email").fill("operator@example.test");
      await page.getByLabel("Password").fill("incorrect-password");
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page.getByRole("alert")).toHaveText("Email or password is incorrect.");
      const after = await form.evaluate((element) => element.getBoundingClientRect().height);
      expect(after).toBe(before);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical").map((violation) => violation.id), `login error axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `login-error-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
    }
  }
});

test("MCP consent route includes its incomplete-link state in the responsive axe sweep", async ({ page }) => {
  test.skip(!email || !password, "The U4 screenshot runner supplies fresh one-time credentials.");
  await signIn(page);
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/mcp/oauth/consent");
      await page.evaluate((selectedTheme) => { document.documentElement.dataset.theme = selectedTheme; }, theme);
      await expect(page).toHaveTitle("MCP access · Lynewright");
      await expect(page.getByRole("heading", { name: "Connect an MCP application" })).toBeVisible();
      await expect(page.getByRole("alert")).toContainText("authorization link is incomplete");
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `OAuth consent axe at ${viewport.width}px ${theme}`).toEqual([]);
      const layout = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
      expect(layout.width, `OAuth consent has no horizontal overflow at ${viewport.width}px ${theme}`).toBeLessThanOrEqual(layout.viewport + 1);
      await page.screenshot({ path: path.join(output, `oauth-consent-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
    }
  }
});

test("captures the Add integration chooser at every responsive breakpoint", async ({ page }) => {
  test.skip(!email || !password, "The U4 screenshot runner supplies fresh one-time credentials.");
  await signIn(page);
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(viewports[0]);
    await setTheme(page, theme);
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/integrations");
      await page.getByRole("button", { name: "Add integration" }).first().click();
      const chooser = page.getByRole("dialog", { name: "Add integration" });
      await expect(chooser).toBeVisible();
      for (const option of ["From catalogue", "Import file", "From link or docs", "Manual"]) await expect(chooser.getByText(option, { exact: true })).toBeVisible();
      const bounds = await chooser.boundingBox();
      expect(bounds && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, `Add integration dialog fits ${viewport.width}x${viewport.height}`).toBe(true);
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `Add integration chooser axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `integration-add-chooser-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      await chooser.getByRole("button", { name: "Close dialog" }).click();
    }
  }
});

test("captures Settings diagnostics and editor states with live API data", async ({ page }) => {
  test.setTimeout(600_000);
  test.skip(!email || !password, "The U4 screenshot runner supplies fresh one-time credentials.");
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  await signIn(page);
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(viewports[0]);
    await setTheme(page, theme);
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/settings");
      const diagnostics = page.locator("details.settings-diagnostics");
      await diagnostics.locator("summary").click();
      await expect(diagnostics).toHaveAttribute("open", "");
      let axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `provider diagnostics axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `settings-provider-diagnostics-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });

      await page.getByRole("button", { name: "Add provider" }).click();
      const providerDialog = page.getByRole("dialog", { name: "Add AI provider" });
      await expect(providerDialog).toBeVisible();
      let dialogBox = await providerDialog.boundingBox();
      expect(dialogBox && dialogBox.x >= 0 && dialogBox.y >= 0 && dialogBox.x + dialogBox.width <= viewport.width + 1 && dialogBox.y + dialogBox.height <= viewport.height + 1, `provider dialog fits ${viewport.width}x${viewport.height}`).toBe(true);
      axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `provider editor axe ${viewport.width}px ${theme}`).toEqual([]);
      await page.screenshot({ path: path.join(output, `settings-provider-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      await providerDialog.getByRole("button", { name: "Close dialog" }).click();

      await page.goto("/settings/operator");
      const addProfile = page.getByRole("button", { name: "Define your own" }).or(page.getByRole("button", { name: "Add an Operator profile" })).first();
      if (await addProfile.isVisible().catch(() => false)) {
        await addProfile.click();
        const profileDialog = page.getByRole("dialog", { name: /Add an agent role|Add Operator profile/ });
        await expect(profileDialog).toBeVisible();
        dialogBox = await profileDialog.boundingBox();
        expect(dialogBox && dialogBox.x >= 0 && dialogBox.y >= 0 && dialogBox.x + dialogBox.width <= viewport.width + 1 && dialogBox.y + dialogBox.height <= viewport.height + 1, `operator profile dialog fits ${viewport.width}x${viewport.height}`).toBe(true);
        axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `operator profile editor axe ${viewport.width}px ${theme}`).toEqual([]);
        await page.screenshot({ path: path.join(output, `settings-operator-profile-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
        await profileDialog.getByRole("button", { name: "Close dialog" }).click();
      }
      const addAutomation = page.getByRole("button", { name: "Add an automation" });
      if (await addAutomation.isVisible().catch(() => false)) {
        await addAutomation.click();
        const automationDialog = page.getByRole("dialog", { name: "Add an automation" });
        await expect(automationDialog).toBeVisible();
        dialogBox = await automationDialog.boundingBox();
        expect(dialogBox && dialogBox.x >= 0 && dialogBox.y >= 0 && dialogBox.x + dialogBox.width <= viewport.width + 1 && dialogBox.y + dialogBox.height <= viewport.height + 1, `automation dialog fits ${viewport.width}x${viewport.height}`).toBe(true);
        axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `automation editor axe ${viewport.width}px ${theme}`).toEqual([]);
        await page.screenshot({ path: path.join(output, `settings-automation-dialog-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
        await automationDialog.getByRole("button", { name: "Close dialog" }).click();
      }

      await page.goto("/settings/telegram");
      const addConnection = page.getByRole("button", { name: "Add a Telegram connection" });
      if (await addConnection.isVisible().catch(() => false)) {
        await addConnection.click();
        await expect(page.getByLabel("Name", { exact: true })).toBeVisible();
        await expect(page.getByLabel("Bot token", { exact: true })).toBeVisible();
        axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `Telegram connection editor axe ${viewport.width}px ${theme}`).toEqual([]);
        await page.screenshot({ path: path.join(output, `settings-telegram-connection-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
      }

      await page.goto("/settings/logs");
      const logDetails = page.locator("details.log-details").first();
      if (await logDetails.count()) {
        await logDetails.locator("summary").click();
        axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `expanded log detail axe ${viewport.width}px ${theme}`).toEqual([]);
        await page.screenshot({ path: path.join(output, `settings-log-detail-${viewport.width}x${viewport.height}-${theme}.png`), animations: "disabled" });
      }
    }
  }
});
