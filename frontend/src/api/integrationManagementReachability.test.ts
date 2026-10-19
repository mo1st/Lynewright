import { fileURLToPath } from "node:url";
import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

const clientSource = readFileSync(fileURLToPath(new URL("./integrationManagementClient.ts", import.meta.url)), "utf8");
const componentDirectory = fileURLToPath(new URL("../components/integrations/", import.meta.url));
const renderedSource = [
  readFileSync(fileURLToPath(new URL("../pages/Integrations.tsx", import.meta.url)), "utf8"),
  ...readdirSync(componentDirectory).filter((name) => name.endsWith(".tsx")).map((name) => readFileSync(`${componentDirectory}/${name}`, "utf8")),
].join("\n");

// Indirect calls must be named here with a reason. The empty list is the
// intended starting point: every management endpoint has a visible control.
const EXEMPTIONS: Record<string, string> = {
  oauthCallback: "The external provider redirects to its registered callback URL; the dashboard does not submit this backend-handled callback.",
  operations: "The integration detail uses the project-scoped production-line operation endpoint, which is the same source the line builder uses.",
};

describe("integration management client reachability", () => {
  it("renders a control for every exported integration-management method", () => {
    const methods = [...clientSource.matchAll(/^  ([A-Za-z][A-Za-z0-9]*)\(/gm)].map((match) => match[1]);
    for (const method of methods) {
      if (EXEMPTIONS[method]) continue;
      expect(renderedSource, `${method} needs a rendered control or a documented exemption`).toContain(`integrationManagementApi.${method}`);
    }
    expect(Object.keys(EXEMPTIONS)).toEqual(["oauthCallback", "operations"]);
  });
});
