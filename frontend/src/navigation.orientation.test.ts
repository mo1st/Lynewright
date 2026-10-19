import { readdirSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SRC_ROOT = resolve(process.cwd(), "src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return entry.isFile() && path.endsWith(".tsx") ? [path] : [];
  });
}

function attributes(node: ts.JsxOpeningLikeElement): Map<string, string | null> {
  const result = new Map<string, string | null>();
  for (const property of node.attributes.properties) {
    if (!ts.isJsxAttribute(property) || !ts.isIdentifier(property.name)) continue;
    const value = property.initializer;
    result.set(property.name.text, value ? ts.isStringLiteral(value) ? value.text : value.getText() : null);
  }
  return result;
}

describe("navigation orientation guard", () => {
  it("allows vertical navigation only in the app Sidebar", () => {
    const verticalLandmarks: string[] = [];
    for (const path of sourceFiles(SRC_ROOT)) {
      const text = readFileSync(path, "utf8");
      const file = relative(SRC_ROOT, path).replaceAll("\\", "/");
      const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const inspect = (node: ts.Node) => {
        if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
          const tag = ts.isIdentifier(node.tagName) ? node.tagName.text : "";
          const props = attributes(node);
          const role = props.get("role");
          const orientation = props.get("aria-orientation");
          const explicitHorizontal = props.get("data-nav-orientation") === "horizontal" || orientation === '"horizontal"';
          const implicitNavigation = tag === "nav" || role === "navigation";
          const verticalTabList = role === "tablist" && orientation === '"vertical"';
          const sidebarNav = file === "components/ui/Layout.tsx" && tag === "nav" && String(props.get("className") ?? "").includes("sidebarNav");
          if ((verticalTabList || (implicitNavigation && !explicitHorizontal)) && !sidebarNav) verticalLandmarks.push(`${file}:${node.getStart(tree)}`);
          if (sidebarNav) verticalLandmarks.push("app Sidebar");
        }
        ts.forEachChild(node, inspect);
      };
      inspect(tree);
    }
    expect(verticalLandmarks).toEqual(["app Sidebar"]);
  });
});
