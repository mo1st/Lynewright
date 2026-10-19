/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getApplicationLogs = vi.fn();
const projectContext = vi.hoisted(() => ({ projectId: "", projects: [] as Array<{ project_id: string; name?: string }>, loading: true }));

vi.mock("../api/client", () => ({ api: { getApplicationLogs } }));
vi.mock("../context/ProjectContext", () => ({ useProject: () => projectContext }));

const { default: ApplicationLogs } = await import("./ApplicationLogs");

describe("ApplicationLogs project readiness", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    projectContext.projectId = "";
    projectContext.projects = [];
    projectContext.loading = true;
    getApplicationLogs.mockResolvedValue({ items: [], total: 0, counts: {} });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("does not query or poll application logs while project resolution is pending", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<ApplicationLogs />);
    });

    expect(getApplicationLogs).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("Project is required.");
  });

  it("loads after a project becomes available", async () => {
    projectContext.projectId = "demo-project";
    projectContext.loading = false;

    await act(async () => {
      root = createRoot(host);
      root.render(<ApplicationLogs />);
      await Promise.resolve();
    });

    expect(getApplicationLogs).toHaveBeenCalledWith({ level: "", category: "", search: "" });
  });
});
