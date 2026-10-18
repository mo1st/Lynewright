import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import IntegrationExecutionBadge from "./IntegrationExecutionBadge";

describe("IntegrationExecutionBadge", () => {
  it("shows the runner connection state for container integrations", () => {
    const markup = renderToStaticMarkup(createElement(IntegrationExecutionBadge, {
      executionPlane: "container", runnerStatus: "connected",
    }));
    expect(markup).toContain("Container runner · connected");
  });

  it("shows the explicit trust warning for in-process packages", () => {
    const markup = renderToStaticMarkup(createElement(IntegrationExecutionBadge, {
      executionPlane: "direct", trustedInprocess: true,
    }));
    expect(markup).toContain("in-process (trusted)");
  });

  it("defaults an unseen runner to never_seen", () => {
    const markup = renderToStaticMarkup(createElement(IntegrationExecutionBadge, { executionPlane: "container" }));
    expect(markup).toContain("Container runner · never_seen");
  });
});
