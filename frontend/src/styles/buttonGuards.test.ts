import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sharedUiRoot = path.join(sourceRoot, "components", "ui");
const oldVariants = /(?:secondary-button|primary-button|danger-button|provider-row-action)/g;

function walk(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(filename) : /\.tsx$/.test(filename) && !/\.test\.tsx$/.test(filename) ? [filename] : [];
  });
}

describe("shared button usage", () => {
  it("keeps raw buttons and legacy button variants inside the U0 components", () => {
    const violations = walk(sourceRoot)
      .filter((filename) => !filename.startsWith(sharedUiRoot))
      .flatMap((filename) => {
        const source = fs.readFileSync(filename, "utf8");
        const relative = path.relative(sourceRoot, filename).replaceAll("\\", "/");
        const rawButtons = Array.from(source.matchAll(/<button\b/g), (match) => `${relative}: raw <button>`);
        const oldClasses = Array.from(source.matchAll(oldVariants), (match) => `${relative}: ${match[0]}`);
        return [...rawButtons, ...oldClasses];
      });
    expect(violations).toEqual([]);
  });
});
