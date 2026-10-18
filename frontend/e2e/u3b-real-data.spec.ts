import fs from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

type LegacyCssCoverage = {
  session: Awaited<ReturnType<Page["context"]>["newCDPSession"]>;
  styleSheetIds: Set<string>;
};
const legacyCssCoverage = new WeakMap<Page, LegacyCssCoverage>();

test.beforeEach(async ({ page }) => {
  if (process.env.LYNEWRIGHT_CSS_COVERAGE !== "1") return;
  const session = await page.context().newCDPSession(page);
  const styleSheetIds = new Set<string>();
  session.on("CSS.styleSheetAdded", (event: { header: { styleSheetId: string } }) => styleSheetIds.add(event.header.styleSheetId));
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
      } catch { /* Ignore browser-owned and cross-origin stylesheets. */ }
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
    fs.appendFileSync(output, `${JSON.stringify({ totalRules, rules: rules.map(({ startOffset, endOffset, used }) => ({
      startOffset, endOffset, used,
      cssText: [...legacyStyleSheetText.values()][0]?.slice(startOffset, endOffset) ?? "",
    })) })}\n`, "utf8");
  } finally {
    await measurement.session.detach();
    legacyCssCoverage.delete(page);
  }
});

const email = process.env.LYNEWRIGHT_SCREENSHOT_EMAIL ?? "";
const password = process.env.LYNEWRIGHT_SCREENSHOT_PASSWORD ?? "";

async function signIn(page: Page) {
  await page.goto("/overview");
  if (await page.getByLabel("Email").isVisible().catch(() => false)) {
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.locator("#main-content")).toBeVisible();
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
    const parsed = text ? JSON.parse(text) : undefined;
    if (!response.ok) throw new Error(`${requestMethod} ${requestUrl}: HTTP ${response.status}: ${text.slice(0, 500)}`);
    return parsed as T;
  }, { requestUrl: url, requestMethod: method, payload: body });
}

async function rawImport(page: Page, text: string) {
  return page.evaluate(async (raw) => {
    const token = localStorage.getItem("lynewright.session-token") ?? "";
    const tenant = localStorage.getItem("lynewright.active-tenant") ?? "";
    const project = localStorage.getItem("lynewright.active-project") ?? "";
    const response = await fetch("/api/integration-management/import", { method: "POST", headers: {
      Authorization: `Bearer ${token}`, "X-Tenant-ID": tenant, "X-Project-ID": project, "Content-Type": "application/yaml",
    }, body: raw });
    return { status: response.status, payload: await response.json() as Record<string, unknown> };
  }, text);
}

function exampleFile(fileName: string, id: string, name: string, endpoint: string, internalRef: string) {
  const source = fs.readFileSync(path.resolve("../backend/app/tests/fixtures/integration_files", fileName), "utf8");
  return source
    .replace(/^id: .*$/m, `id: ${id}`)
    .replace(/^name: .*$/m, `name: ${name}`)
    .replace(/^endpoint_url: .*$/m, `endpoint_url: ${endpoint}`)
    + `\ninternal_endpoint_ref: ${internalRef}\n`;
}

test("U3c horizontal navigation walkthrough uses API-seeded integrations", async ({ page }) => {
  test.setTimeout(360_000);
  test.skip(!email || !password, "The U3c screenshot runner supplies one-time credentials and a fresh data directory.");
  const output = path.resolve(process.env.LYNEWRIGHT_SCREENSHOT_DIR ?? "e2e/screens");
  const openApiOrigin = process.env.LYNEWRIGHT_SCREENSHOT_OPENAPI_ORIGIN ?? "http://127.0.0.1:8080";
  fs.mkdirSync(output, { recursive: true });
  const pageErrors: string[] = [];
  const apiFailures: string[] = [];
  const apiTraffic: Array<{ method: string; path: string; status: number }> = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (!response.url().includes("/api/")) return;
    const request = response.request();
    const item = { method: request.method(), path: new URL(response.url()).pathname, status: response.status() };
    apiTraffic.push(item);
    if ([404, 500, 502, 503].includes(item.status)) {
      const summary = `${item.status} ${item.method} ${item.path}`;
      apiFailures.push(summary);
      void response.text().then((body) => {
        const index = apiFailures.indexOf(summary);
        if (index >= 0) apiFailures[index] = `${summary}: ${body.slice(0, 300)}`;
      }).catch(() => undefined);
    }
  });

  await signIn(page);
  await expect.poll(() => page.locator("#project-picker option").count(), { timeout: 30_000 }).toBeGreaterThan(1);
  const projectOptions = await page.locator("#project-picker option").evaluateAll((options) => options.map((item) => ({ value: (item as HTMLOptionElement).value, label: item.textContent ?? "" })));
  const projectId = projectOptions.find((item) => item.value === "lynewright-demo-media")?.value ?? projectOptions.find((item) => item.value)?.value;
  expect(projectId, "the demo bootstrap should provide a selectable project").toBeTruthy();
  await page.locator("#project-picker").selectOption(projectId!);

  const refreshed = await dashboardApi<{ status: Record<string, unknown>; entries: Array<Record<string, unknown>> }>(page, "/api/catalogue/refresh", "POST", {});
  expect(refreshed.entries.some((entry) => entry.id === "lynewright.integration.example-native" && entry.kind === "package"), "the signed local catalogue has its native package entry").toBe(true);
  const installPath = "/api/catalogue/entries/lynewright.integration.example-native/1.0.0/install";
  const packageInstall = await dashboardApi<{ integration: { integration: { id: string } } }>(page, installPath, "POST", {});
  expect(packageInstall.integration.integration.id).toBe("lynewright.integration.example-native");
  await dashboardApi(page, "/api/integration-management/lynewright.integration.example-native/enable", "POST", {});

  const openApiText = exampleFile("openapi.yaml", "lynewright.integration.u3b-openapi", "U3b OpenAPI Example", openApiOrigin, "u3b_openapi");
  const openApiFirst = await rawImport(page, openApiText);
  expect(openApiFirst.status).toBe(409);
  const openApiDetail = openApiFirst.payload.detail as { install_summary: { integration_id: string }; summary_hash: string };
  const openApiConfirmation = await page.evaluate(async ({ raw, confirmation }) => {
    const token = localStorage.getItem("lynewright.session-token") ?? "";
    const tenant = localStorage.getItem("lynewright.active-tenant") ?? "";
    const project = localStorage.getItem("lynewright.active-project") ?? "";
    const response = await fetch("/api/integration-management/import", { method: "POST", headers: {
      Authorization: `Bearer ${token}`, "X-Tenant-ID": tenant, "X-Project-ID": project, "Content-Type": "application/yaml",
      "X-Lynewright-Install-Confirm": JSON.stringify(confirmation),
    }, body: raw });
    return { status: response.status, payload: await response.json() as { integration?: { integration?: { id?: string } } } };
  }, { raw: openApiText, confirmation: { integration_id: openApiDetail.install_summary.integration_id, summary_hash: openApiDetail.summary_hash } });
  expect(openApiConfirmation.status, JSON.stringify(openApiConfirmation.payload)).toBe(201);

  const mcpText = exampleFile("mcp.yaml", "lynewright.integration.u3b-mcp", "U3b MCP Example", `${openApiOrigin}/mcp`, "u3b_openapi");
  const mcpFirst = await rawImport(page, mcpText);
  expect(mcpFirst.status).toBe(409);
  const mcpDetail = mcpFirst.payload.detail as { install_summary: { integration_id: string }; summary_hash: string };
  const mcpConfirmation = await page.evaluate(async ({ raw, confirmation }) => {
    const token = localStorage.getItem("lynewright.session-token") ?? "";
    const tenant = localStorage.getItem("lynewright.active-tenant") ?? "";
    const project = localStorage.getItem("lynewright.active-project") ?? "";
    const response = await fetch("/api/integration-management/import", { method: "POST", headers: {
      Authorization: `Bearer ${token}`, "X-Tenant-ID": tenant, "X-Project-ID": project, "Content-Type": "application/yaml",
      "X-Lynewright-Install-Confirm": JSON.stringify(confirmation),
    }, body: raw });
    return { status: response.status };
  }, { raw: mcpText, confirmation: { integration_id: mcpDetail.install_summary.integration_id, summary_hash: mcpDetail.summary_hash } });
  expect(mcpConfirmation.status).toBe(201);

  const summaries = await dashboardApi<{ items: Array<{ integration: { id: string; name: string }; provenance?: { kind?: string } }> }>(page, "/api/integration-management");
  const writer = summaries.items.find((item) => /demo writer/i.test(item.integration.name));
  const packageItem = summaries.items.find((item) => item.integration.id === "lynewright.integration.example-native");
  const openApiItem = summaries.items.find((item) => item.integration.id === "lynewright.integration.u3b-openapi");
  const mcpItem = summaries.items.find((item) => item.integration.id === "lynewright.integration.u3b-mcp");
  expect(writer, "the live demo plugin exposes Demo Writer").toBeTruthy();
  expect(packageItem, "the package was installed through the signed catalogue route").toBeTruthy();
  expect(packageItem?.provenance?.kind).toBe("catalogue");
  expect(openApiItem, "the OpenAPI example was imported through the real file endpoint").toBeTruthy();
  expect(mcpItem, "the MCP example was imported through the real file endpoint").toBeTruthy();

  const createdDraft = await dashboardApi<{ id: string; draft_json: Record<string, unknown> }>(page, "/api/integration-management/drafts", "POST", {
    source_kind: "openapi_url", project_id: projectId, url: `${openApiOrigin}/openapi.json`, internal_endpoint_ref: "u3b_openapi",
  });
  const draftJson = structuredClone(createdDraft.draft_json);
  const draftOperations = Array.isArray(draftJson.operations) ? draftJson.operations as Array<Record<string, unknown>> : [];
  expect(draftOperations[0]?.operation_id).toBe("health");
  draftOperations[0] = { ...draftOperations[0], enabled: true };
  await dashboardApi(page, `/api/integration-management/drafts/${encodeURIComponent(createdDraft.id)}`, "PUT", { project_id: projectId, draft_json: draftJson });
  const probedDraft = await dashboardApi<{ status: string; probe_result: Record<string, unknown> }>(page, `/api/integration-management/drafts/${encodeURIComponent(createdDraft.id)}/probe`, "POST", {
    project_id: projectId, operation_id: "health", args: {},
  });
  expect(probedDraft.status).toBe("probed");
  expect(probedDraft.probe_result.status).toBe("passed");

  const operationsResponse = await dashboardApi<{ items: Array<Record<string, unknown>> }>(page, `/api/production-lines/operations?project_id=${encodeURIComponent(projectId!)}`);
  const liveOperations = operationsResponse.items.filter((item) => [writer!.integration.id, packageItem!.integration.id].includes(String(item.integration_id)));
  expect(liveOperations.some((item) => item.integration_id === writer!.integration.id), "Demo Writer operations are visible through the production-line API").toBe(true);
  expect(liveOperations.some((item) => item.integration_id === packageItem!.integration.id), "catalogue package operations are visible through the production-line API").toBe(true);
  const capturedFixture = path.join(output, "u3b-line-operations.real.json");
  fs.writeFileSync(capturedFixture, `${JSON.stringify({ items: liveOperations }, null, 2)}\n`, "utf8");

  const capture = async (filename: string) => {
    const dimensions = await page.evaluate(() => ({
      width: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      offenders: Array.from(document.querySelectorAll("body *")).map((element) => {
        const rect = element.getBoundingClientRect();
        return { tag: element.tagName, id: (element as HTMLElement).id, className: typeof (element as HTMLElement).className === "string" ? (element as HTMLElement).className : "", right: Math.round(rect.right), width: Math.round(rect.width), scrollWidth: (element as HTMLElement).scrollWidth, text: element.textContent?.trim().slice(0, 70) ?? "" };
      }).filter((item) => item.width > 0 && item.right > window.innerWidth + 1).slice(0, 12),
    }));
    expect(dimensions.width, `no horizontal overflow for ${filename}; offenders=${JSON.stringify(dimensions.offenders)}`).toBeLessThanOrEqual(dimensions.viewport + 1);
    await page.screenshot({ path: path.join(output, filename), animations: "disabled" });
  };
  const sizes = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 768, height: 900 }, { width: 375, height: 812 }];
  const navigateDetail = async (id: string, section?: string) => {
    const targetSection = section ?? "Overview";
    const sectionSlug = ({ Overview: "overview", Operations: "operations" } as Record<string, string>)[targetSection];
    expect(sectionSlug, `section slug exists for ${targetSection}`).toBeTruthy();
    await page.goto(`/integrations/${encodeURIComponent(id)}/${sectionSlug}`);
    await expect(page).toHaveURL(new RegExp(`/integrations/${encodeURIComponent(id)}/${sectionSlug}$`));
    await expect(page.getByRole("tablist", { name: "Integration sections" }).getByRole("tab", { name: targetSection, exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: targetSection, exact: true })).toBeVisible();
  };

  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(sizes[0]);
    const account = page.getByRole("button", { name: /@example\.test$/ });
    const themeGroup = page.getByRole("group", { name: "Theme" });
    if (!await themeGroup.isVisible().catch(() => false)) await account.click();
    await themeGroup.getByRole("button", { name: theme === "light" ? "Light" : "Dark", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    if (await themeGroup.isVisible().catch(() => false)) await account.click();
    for (const viewport of sizes) {
      await page.setViewportSize(viewport);
      for (const [label, id] of [["demo-writer", writer!.integration.id], ["package", packageItem!.integration.id]] as const) {
        for (const section of ["Overview", "Operations"] as const) {
          await navigateDetail(id, section);
          if (section === "Operations") await expect(page.getByText("Available operations")).toBeVisible();
          const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
          expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `integration ${label} ${section} axe at ${viewport.width}px ${theme}`).toEqual([]);
          await capture(`u3c-integration-${label}-${section.toLowerCase()}-${viewport.width}x${viewport.height}-${theme}.png`);
        }
      }
    }
  }

  // U5 removal proof uses the real package already installed above, a line
  // definition saved through the production-lines API, and the actual dialog
  // and removal endpoints. The line's async stage declares the package's
  // render/status pair rather than relying on a fixture-only integration.
  const packageRender = liveOperations.find((item) => item.integration_id === packageItem!.integration.id && item.operation === "render");
  expect(packageRender, "the native package exposes render for a production line").toBeTruthy();
  const removalLineId = "u5-package-removal-check";
  await dashboardApi(page, "/api/production-lines", "POST", {
    id: removalLineId, name: "U5 package removal check", project_id: projectId, enabled: true,
    definition: { stages: [{
      id: "render", kind: "async_operation", operation_id: packageRender!.operation_id,
      contract: packageRender!.contract, operation: "render", args_template: { prompt: "U5 removal check" },
      status_operation: { contract: packageRender!.contract, operation: "status" },
      status_args_template: { job_id: "{{stages.render.output.job_id}}" },
      async_ref_path: "job_id", status_ref_field: "job_id",
    }] },
  });
  const beforeRemoval = await dashboardApi<{ lines: Array<{ id: string }>; blocked: boolean; blocked_reasons: string[]; disable_lines_blocked_reasons: string[] }>(
    page, `/api/integration-management/${encodeURIComponent(packageItem!.integration.id)}/removal-preview`,
  );
  expect(beforeRemoval.lines.some((line) => line.id === removalLineId), "removal preview lists the line using the package operation").toBe(true);
  expect(beforeRemoval.blocked).toBe(true);
  expect(beforeRemoval.blocked_reasons.some((reason) => reason.includes(removalLineId) && reason.includes("disable those lines"))).toBe(true);
  expect(beforeRemoval.disable_lines_blocked_reasons).toEqual([]);

  const removalSizes = [{ width: 1440, height: 900 }, { width: 375, height: 812 }];
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(removalSizes[0]);
    const account = page.getByRole("button", { name: /@example\.test$/ });
    const themeGroup = page.getByRole("group", { name: "Theme" });
    if (!await themeGroup.isVisible().catch(() => false)) await account.click();
    await themeGroup.getByRole("button", { name: theme === "light" ? "Light" : "Dark", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    if (await themeGroup.isVisible().catch(() => false)) await account.click();
    for (const viewport of removalSizes) {
      await page.setViewportSize(viewport);
      await navigateDetail(packageItem!.integration.id, "Overview");
      await page.locator("#main-content details > summary").filter({ hasText: /^More\b/ }).click();
      await page.getByRole("menuitem", { name: "Remove", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: `Remove ${packageItem!.integration.name}?` });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByText("U5 package removal check")).toBeVisible();
      const removeButton = dialog.getByRole("button", { name: "Remove integration" });
      expect(await removeButton.isDisabled()).toBe(true);
      const dialogBox = await dialog.boundingBox();
      expect(dialogBox, "blocked integration preview is in the viewport").toBeTruthy();
      expect(dialogBox!.x).toBeGreaterThanOrEqual(0);
      expect(dialogBox!.x + dialogBox!.width).toBeLessThanOrEqual(viewport.width + 1);
      await capture(`u5-integration-removal-blocked-${viewport.width}x${viewport.height}-${theme}.png`);

      await dialog.getByLabel("Disable these lines, then remove the integration").check();
      await expect(dialog.getByLabel("Copy Integration ID")).toBeVisible();
      await dialog.getByLabel("Type the integration ID to confirm").fill(packageItem!.integration.id);
      expect(await removeButton.isEnabled()).toBe(true);
      const confirmAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(confirmAxe.violations.filter((item) => item.impact === "serious" || item.impact === "critical").map((item) => item.id), `U5 removal dialog ${viewport.width}px ${theme}`).toEqual([]);
      await capture(`u5-integration-removal-allowed-${viewport.width}x${viewport.height}-${theme}.png`);
      await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    }
  }

  // Perform the destructive half once after capturing both dialog states.
  await navigateDetail(packageItem!.integration.id, "Overview");
  await page.locator("#main-content details > summary").filter({ hasText: /^More\b/ }).click();
  await page.getByRole("menuitem", { name: "Remove", exact: true }).click();
  const removalDialog = page.getByRole("dialog", { name: `Remove ${packageItem!.integration.name}?` });
  await removalDialog.getByLabel("Disable these lines, then remove the integration").check();
  await removalDialog.getByLabel(`Type ${packageItem!.integration.id} to confirm`).fill(packageItem!.integration.id);
  await removalDialog.getByRole("button", { name: "Remove integration" }).click();
  await expect(page).toHaveURL(/\/integrations$/);
  const afterRemoval = await dashboardApi<{ items: Array<{ integration: { id: string } }> }>(page, "/api/integration-management");
  expect(afterRemoval.items.some((item) => item.integration.id === packageItem!.integration.id)).toBe(false);
  const disabledLine = await dashboardApi<{ items: Array<{ id: string; enabled: boolean }> }>(
    page, `/api/production-lines?project_id=${encodeURIComponent(projectId!)}&include_shared=true`,
  );
  expect(disabledLine.items.find((line) => line.id === removalLineId)?.enabled).toBe(false);

  const installableAgain = await dashboardApi<{ entries: Array<{ id: string; installed?: boolean }> }>(page, "/api/catalogue");
  expect(installableAgain.entries.find((entry) => entry.id === packageItem!.integration.id)?.installed).toBe(false);
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize(removalSizes[0]);
    const account = page.getByRole("button", { name: /@example\.test$/ });
    const themeGroup = page.getByRole("group", { name: "Theme" });
    if (!await themeGroup.isVisible().catch(() => false)) await account.click();
    await themeGroup.getByRole("button", { name: theme === "light" ? "Light" : "Dark", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    if (await themeGroup.isVisible().catch(() => false)) await account.click();
    for (const viewport of removalSizes) {
      await page.setViewportSize(viewport);
      await page.goto("/catalogue");
      const card = page.locator("article").filter({ hasText: "Native Python Example" }).first();
      await expect(card.getByRole("button", { name: "Install", exact: true })).toBeVisible();
      await capture(`u5-catalogue-installable-again-${viewport.width}x${viewport.height}-${theme}.png`);
    }
  }
  const reinstalled = await dashboardApi<{ installed: boolean }>(
    page, "/api/catalogue/entries/lynewright.integration.example-native/1.0.0/install", "POST", {},
  );
  expect(reinstalled.installed).toBe(true);
  await dashboardApi(page, "/api/integration-management/lynewright.integration.example-native/enable", "POST", {});

  fs.writeFileSync(path.join(output, "api-traffic.json"), `${JSON.stringify(apiTraffic, null, 2)}\n`, "utf8");
  await page.waitForTimeout(50);
  expect(pageErrors, "U3c browser page errors").toEqual([]);
  expect(apiFailures, "real API requests return no 404/5xx responses").toEqual([]);
  console.log(`U3c screenshots: ${output}`);
  console.log(`Captured production-line operations fixture from the live API: ${capturedFixture}`);
  console.log("Route mocks: none");
});
