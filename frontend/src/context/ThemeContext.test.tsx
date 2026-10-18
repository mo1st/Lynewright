/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "./ThemeContext";

function Controls() {
  const { preference, resolvedTheme, setPreference, setUserId } = useTheme();
  return <div><output>{preference}/{resolvedTheme}</output><button onClick={() => setUserId("101")}>User one</button><button onClick={() => setUserId("202")}>User two</button><button onClick={() => setPreference("light")}>Set light</button><button onClick={() => setPreference("dark")}>Set dark</button></div>;
}

describe("per-user theme preference", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host);
    window.localStorage.clear();
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  });
  afterEach(() => { act(() => root?.unmount()); host.remove(); vi.unstubAllGlobals(); });

  it("follows the system by default and stores manual choices under each user id", async () => {
    window.localStorage.setItem("lynewright_theme:101", "dark");
    window.localStorage.setItem("lynewright_theme:202", "light");
    await act(async () => { root = createRoot(host); root.render(<ThemeProvider><Controls /></ThemeProvider>); });
    expect(host.querySelector("output")?.textContent).toBe("system/dark");
    await act(async () => { host.querySelectorAll("button")[0].click(); await Promise.resolve(); });
    expect(host.querySelector("output")?.textContent).toBe("dark/dark");
    await act(async () => { host.querySelectorAll("button")[2].click(); });
    expect(window.localStorage.getItem("lynewright_theme:101")).toBe("light");
    await act(async () => { host.querySelectorAll("button")[1].click(); await Promise.resolve(); });
    expect(host.querySelector("output")?.textContent).toBe("light/light");
    await act(async () => { host.querySelectorAll("button")[3].click(); });
    expect(window.localStorage.getItem("lynewright_theme:202")).toBe("dark");
  });
});
