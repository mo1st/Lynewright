/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardSummary } from "../useDashboardSummary";

vi.mock("../context/ProjectContext", () => ({ useProject: () => ({ projectId: "project-default", projects: [{ project_id: "project-default", name: "Default Project" }] }) }));
const { default: Overview } = await import("./Overview");

function summary(overrides: Partial<DashboardSummary> = {}): DashboardSummary {
  return { lines: [], runs: [], approvals: [], integrations: [], loading: false, linesError: "", runsError: "", approvalsError: "", integrationsError: "", ...overrides };
}

describe("Overview attention list", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => { (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true; window.history.replaceState({}, "", "/overview"); host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
  afterEach(() => { act(() => root.unmount()); document.body.innerHTML = ""; });

  it("shows the clear state without also rendering attention rows", async () => {
    await act(async () => { root.render(<Overview summary={summary()} />); });
    expect(host.textContent).toContain("Nothing needs attention");
    expect(host.querySelector('[aria-label="Items needing attention"]')).toBeNull();
  });

  it("shows only non-zero attention rows when work needs attention", async () => {
    const failed = { run_id: "run-1", status: "failed", created_at: Math.floor(Date.now() / 1000) } as never;
    const partial = { run_id: "run-2", status: "done_with_errors", created_at: Math.floor(Date.now() / 1000) } as never;
    await act(async () => { root.render(<Overview summary={summary({ runs: [failed, partial] })} />); });
    expect(host.textContent).not.toContain("Nothing needs attention");
    const rows = Array.from(host.querySelectorAll('[aria-label="Items needing attention"] button')).map((button) => button.textContent ?? "");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain("Failed runs"); expect(rows[1]).toContain("Runs with item errors");
  });

  it("shows the five most recent runs with line, state, time, and run links", async () => {
    const recent = { id: "run-recent", line_id: "line-1", status: "done", created_at: Math.floor(Date.now() / 1000) } as never;
    await act(async () => { root.render(<Overview summary={summary({ runs: [recent], lines: [{ id: "line-1", name: "Daily check" } as never] })} />); });
    const row = host.querySelector('[aria-label="Recent runs"] button');
    expect(row?.textContent).toContain("Daily check"); expect(row?.textContent).toContain("done");
    await act(async () => { row?.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(window.location.pathname).toBe("/runs/run-recent");
  });

  it("links runner and integration drift attention to the matching integration", async () => {
    const runner = { integration: { id: "lynewright.integration.runner" }, availability: { execution_plane: "container", runner_status: "disconnected" } } as never;
    const drift = { integration: { id: "lynewright.integration.drift" }, availability: { execution_plane: "api", runner_status: "connected" }, provenance: { status: ["cata", "logue_drift"].join("") } } as never;
    await act(async () => { root.render(<Overview summary={summary({ integrations: [runner, drift] })} />); });
    const rows = Array.from(host.querySelectorAll('[aria-label="Items needing attention"] button'));
    const runnerRow = rows.find((row) => row.textContent?.includes("Runners not connected"));
    const driftRow = rows.find((row) => row.textContent?.includes("drift"));
    await act(async () => { runnerRow?.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(window.location.search).toBe("?integration_id=lynewright.integration.runner");
    await act(async () => { driftRow?.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(window.location.search).toBe("?integration_id=lynewright.integration.drift");
  });
});
