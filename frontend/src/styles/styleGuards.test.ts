import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const knownPageStyles = new Set(["styles/tokens.css", "styles/base.css", "pages/ProductionAreas.module.css"]);
function walk(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(filename) : filename.endsWith(".css") ? [filename] : [];
  });
}
function withoutComments(value: string): string { return value.replace(/\/\*[\s\S]*?\*\//g, ""); }

const legacyAliases = new Set([
  "--accent", "--accent-color", "--accent-dark", "--accent-text", "--action-primary", "--action-primary-hover", "--action-secondary",
  "--border", "--border-color", "--border-strong", "--border-subtle", "--canvas", "--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5",
  "--content-max", "--danger", "--duration-press", "--duration-pulse", "--duration-pulse-reduced", "--duration-sheet", "--duration-spin-reduced",
  "--ease-drawer", "--ease-in-out", "--ease-out", "--gradient-action", "--ink", "--mobile-nav-height", "--muted", "--primary-color", "--radius", "--shadow-focus",
  "--sidebar", "--sidebar-active", "--sidebar-collapsed-width", "--sidebar-width", "--signal-cyan", "--signal-teal", "--signal-teal-text",
  "--status-danger", "--status-info", "--status-success", "--status-warning", "--success", "--surface", "--surface-canvas", "--surface-inverse", "--surface-inverse-raised",
  "--surface-muted", "--surface-raised", "--surface-subtle", "--text", "--text-inverse", "--text-inverse-muted", "--text-muted", "--text-primary", "--text-secondary",
  "--tone-active-bg", "--tone-active-ink", "--tone-attention-bg", "--tone-attention-ink", "--tone-critical-bg", "--tone-critical-ink", "--tone-neutral-bg", "--tone-neutral-ink",
  "--tone-positive-bg", "--tone-positive-ink", "--warning",
]);

const cssFiles = walk(sourceRoot);
describe("stylesheet guards", () => {
  it("keeps CSS modules with components and legacy page styles in the known set", () => {
    const disallowed = cssFiles.map((filename) => path.relative(sourceRoot, filename).replaceAll("\\", "/"))
      .filter((filename) => !knownPageStyles.has(filename) && !/^components\/.+\.module\.css$/.test(filename));
    expect(disallowed).toEqual([]);
  });

  it("has no active !important declarations outside a documented allowlist", () => {
    const allowlist: Array<{ file: string; property: string; reason: string }> = [];
    const violations = cssFiles.flatMap((filename) => {
      const source = withoutComments(fs.readFileSync(filename, "utf8"));
      return Array.from(source.matchAll(/([\w-]+)\s*:[^;{}]*!important/g), (match) => `${path.relative(sourceRoot, filename)}: ${match[1]}`);
    });
    expect(violations).toEqual(allowlist.map((item) => `${item.file}: ${item.property}`));
    expect(allowlist.every((item) => item.reason.length > 0)).toBe(true);
  });

  it("keeps color literals in tokens.css only", () => {
    const literal = /#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\(/i;
    const violations = cssFiles.filter((filename) => path.relative(sourceRoot, filename).replaceAll("\\", "/") !== "styles/tokens.css")
      .flatMap((filename) => {
        const source = withoutComments(fs.readFileSync(filename, "utf8"));
        return Array.from(source.matchAll(new RegExp(literal.source, "gi")), (match) => `${path.relative(sourceRoot, filename)}: ${match[0]}`);
      });
    expect(violations).toEqual([]);
  });

  it("has no legacy token definitions or usages", () => {
    const tokenCss = fs.readFileSync(path.join(sourceRoot, "styles/tokens.css"), "utf8");
    const definitions = Array.from(tokenCss.matchAll(/(--[\w-]+)\s*:/g), (match) => match[1]).filter((name) => legacyAliases.has(name));
    const usages = walkSource(sourceRoot).flatMap((filename) => {
      const source = fs.readFileSync(filename, "utf8");
      return Array.from(source.matchAll(/var\(\s*(--[\w-]+)/g), (match) => match[1]).filter((name) => legacyAliases.has(name)).map((name) => `${path.relative(sourceRoot, filename)}: ${name}`);
    });
    expect(definitions).toEqual([]);
    expect(usages).toEqual([]);
  });
});

function walkSource(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) return walkSource(filename);
    return /\.(?:css|ts|tsx)$/.test(filename) ? [filename] : [];
  });
}

function tokenMap(selector: string): Record<string, string> {
  const css = fs.readFileSync(path.join(sourceRoot, "styles/tokens.css"), "utf8");
  const selectorStart = css.indexOf(selector);
  const blockStart = css.indexOf("{", selectorStart);
  const blockEnd = css.indexOf("}", blockStart);
  const block = selectorStart < 0 || blockStart < 0 || blockEnd < 0 ? "" : css.slice(blockStart + 1, blockEnd);
  const rule = block ? ["", block] : null;
  if (!rule) throw new Error(`Missing token block ${selector}`);
  return Object.fromEntries(Array.from(rule[1].matchAll(/(--[\w-]+):\s*([^;]+);/g), (match) => [match[1], match[2].trim()]));
}
function luminance(hex: string): number {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((index) => parseInt(value.slice(index, index + 2), 16) / 255).map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}
function contrast(a: string, b: string): number {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((left, right) => right - left);
  return (lighter + 0.05) / (darker + 0.05);
}

describe("semantic token contrast", () => {
  it.each([{ name: "light", selector: "[data-theme=\"light\"]" }, { name: "dark", selector: "[data-theme=\"dark\"]" }])("keeps text, accents, semantic states and borders accessible in $name mode", ({ selector }) => {
    const tokens = tokenMap(selector);
    for (const surface of ["--color-bg", "--color-surface", "--color-surface-raised"]) {
      expect(contrast(tokens["--color-text"], tokens[surface])).toBeGreaterThanOrEqual(4.5);
      expect(contrast(tokens["--color-text-muted"], tokens[surface])).toBeGreaterThanOrEqual(4.5);
      expect(contrast(tokens["--color-border"], tokens[surface])).toBeGreaterThanOrEqual(3);
    }
    expect(contrast(tokens["--color-accent"], tokens["--color-accent-contrast"])).toBeGreaterThanOrEqual(4.5);
    for (const state of ["success", "warning", "danger", "info"]) {
      expect(contrast(tokens[`--color-${state}`], tokens[`--color-${state}-bg`])).toBeGreaterThanOrEqual(4.5);
    }
  });
});
