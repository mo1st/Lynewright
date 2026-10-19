import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Sidebar } from "./Layout";
import { SidebarBrand, LoginBrand } from "./BrandLockup";
import { LogoMark } from "./LogoMark";

describe("LogoMark", () => {
  it("uses the requested geometry, theme color, and size", () => {
    const markup = renderToStaticMarkup(<LogoMark size={48} label="Lynewright" />);
    expect(markup).toContain('viewBox="0 0 56 56"');
    expect(markup).toContain('width="48" height="48"');
    expect(markup).toContain('d="M14 9V45H42"');
    expect(markup).toContain('stroke="currentColor"');
    expect(markup).toContain('fill="var(--color-accent)"');
    expect(markup).toContain('role="img" aria-label="Lynewright"');
  });

  it("renders an accessible decorative mark in the sidebar brand and login brand", () => {
    const sidebar = renderToStaticMarkup(<Sidebar header={<SidebarBrand />}><span>Navigation</span></Sidebar>);
    const login = renderToStaticMarkup(<LoginBrand />);
    for (const markup of [sidebar, login]) {
      expect(markup).toMatch(/<svg[^>]*aria-hidden="true"/);
      expect(markup).toContain("Lynewright");
    }
    expect(login).toContain('width="48" height="48"');
  });
});
