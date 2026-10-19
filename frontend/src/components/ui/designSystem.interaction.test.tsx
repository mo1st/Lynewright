/** @vitest-environment jsdom */
import { act } from "react";
import { useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dialog, SystemBar, Tabs, ToastProvider, useToast, Switch } from "./index";

function Harness() {
  const { addToast } = useToast();
  return <button type="button" onClick={() => addToast({ message: "Saved", tone: "success" })}>Add toast</button>;
}

function FocusReturnHarness() {
  const [open, setOpen] = useState(false);
  return <><button type="button" onClick={() => setOpen(true)}>Open dialog</button><Dialog open={open} title="Confirm" onClose={() => setOpen(false)}><button type="button">Confirm</button></Dialog></>;
}

describe("design system keyboard and state behavior", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host);
  });
  afterEach(() => { act(() => root?.unmount()); host.remove(); vi.restoreAllMocks(); });

  it("moves focus and selection with arrow keys and Home/End in tabs", () => {
    const onChange = vi.fn();
    act(() => { root = createRoot(host); root.render(<Tabs label="Sections" tabs={[{ key: "one", label: "One" }, { key: "two", label: "Two" }, { key: "three", label: "Three", disabled: true }]} active="one" onChange={onChange} />); });
    const tabs = host.querySelectorAll<HTMLButtonElement>("[role=tab]");
    act(() => tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(onChange).toHaveBeenCalledWith("two");
    expect(document.activeElement).toBe(tabs[1]);
    act(() => tabs[1].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
    expect(onChange).toHaveBeenLastCalledWith("two");
  });

  it("scrolls an off-screen active tab into the visible tablist", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute("role") === "tablist") return new DOMRect(0, 0, 100, 48);
      if (this.getAttribute("role") === "tab") {
        const tabs = Array.from(this.parentElement?.querySelectorAll("[role=tab]") ?? []);
        const index = tabs.indexOf(this);
        const left = index * 70 - (this.parentElement?.scrollLeft ?? 0);
        return new DOMRect(left, 0, 55, 40);
      }
      return new DOMRect(0, 0, 0, 0);
    });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.getAttribute("role") === "tablist" ? 100 : 0;
    });
    act(() => { root = createRoot(host); root.render(<Tabs label="Settings sections" tabs={[{ key: "one", label: "One" }, { key: "two", label: "Two" }, { key: "three", label: "Three" }, { key: "assistants", label: "Assistants" }]} active="assistants" onChange={() => undefined} />); });
    const tablist = host.querySelector<HTMLElement>("[role=tablist]")!;
    const active = host.querySelector<HTMLElement>('[role=tab][aria-selected="true"]')!;
    expect(tablist.scrollLeft).toBeGreaterThan(0);
    expect(active.getBoundingClientRect().left).toBeGreaterThanOrEqual(0);
    expect(active.getBoundingClientRect().right).toBeLessThanOrEqual(tablist.clientWidth);
  });

  it("closes dialogs on Escape and traps focus at the ends", async () => {
    const onClose = vi.fn();
    act(() => { root = createRoot(host); root.render(<Dialog open title="Confirm" onClose={onClose}><button type="button">First</button><button type="button">Last</button></Dialog>); });
    const buttons = host.querySelectorAll<HTMLButtonElement>("button");
    await act(async () => { await Promise.resolve(); });
    expect(document.activeElement).toBe(buttons[0]);
    buttons[0].focus();
    act(() => buttons[0].dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true, shiftKey: true })));
    expect(document.activeElement).toBe(buttons[2]);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("returns focus to the opener when a dialog closes", async () => {
    act(() => { root = createRoot(host); root.render(<FocusReturnHarness />); });
    const opener = host.querySelector<HTMLButtonElement>("button")!;
    opener.focus();
    act(() => opener.click());
    await act(async () => { await Promise.resolve(); });
    expect(document.activeElement).toBe(host.querySelector('[role="dialog"] button'));
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await Promise.resolve(); });
    expect(document.activeElement).toBe(opener);
  });

  it("dismisses a system bar for the current browser session", () => {
    window.sessionStorage.clear();
    act(() => { root = createRoot(host); root.render(<SystemBar storageKey="dismiss-test">Notice</SystemBar>); });
    const dismiss = host.querySelector<HTMLButtonElement>("button[aria-label='Dismiss message']")!;
    expect(dismiss.className).toContain("ui-icon-button--ghost");
    act(() => dismiss.click());
    expect(host.textContent).not.toContain("Notice");
    expect(window.sessionStorage.getItem("lynewright.system-bar:dismiss-test")).toBe("dismissed");
  });

  it("renders provider notifications and removes them when dismissed", () => {
    act(() => { root = createRoot(host); root.render(<ToastProvider><Harness /></ToastProvider>); });
    act(() => host.querySelector("button")!.click());
    expect(host.textContent).toContain("Saved");
    act(() => host.querySelector<HTMLButtonElement>("button[aria-label='Dismiss notification']")!.click());
    expect(host.textContent).not.toContain("Saved");
  });

  it("reports switch changes with its native checked value", () => {
    const onChange = vi.fn();
    act(() => { root = createRoot(host); root.render(<Switch label="Enabled" checked={false} onChange={onChange} />); });
    const input = host.querySelector<HTMLInputElement>("[role=switch]")!;
    act(() => { input.click(); });
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
