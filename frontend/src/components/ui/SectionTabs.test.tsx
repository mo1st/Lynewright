import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SectionTabs } from "./SectionTabs";

describe("SectionTabs", () => {
  it("renders an accessible selected tab and its peers", () => {
    const markup = renderToStaticMarkup(<SectionTabs ariaLabel="Line views" tabs={[{ key: "lines", label: "Lines" }, { key: "runs", label: "Runs" }]} active="lines" onChange={() => undefined} />);
    expect(markup).toContain('aria-label="Line views"');
    expect(markup).toContain('aria-selected="true"');
    expect(markup).toContain('role="tablist"');
    expect(markup).toContain("Lines");
    expect(markup).toContain("Runs");
  });
});
