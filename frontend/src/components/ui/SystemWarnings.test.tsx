/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { catalogueApi } from "../../api/catalogueClient";
import { integrationManagementApi } from "../../api/integrationManagementClient";

vi.mock("../../api/catalogueClient", () => ({ catalogueApi: { get: vi.fn() } }));
vi.mock("../../api/integrationManagementClient", () => ({ integrationManagementApi: { list: vi.fn() } }));
const { SystemWarnings } = await import("./SystemWarnings");

describe("system warnings", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    sessionStorage.clear();
    host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
    vi.mocked(catalogueApi.get).mockResolvedValue({ status: { dev_key_trusted: true } } as never);
    vi.mocked(integrationManagementApi.list).mockResolvedValue({ install_policy: "open", items: [], package_statuses: [] } as never);
  });
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks(); });

  it("combines plain-language warnings and persists dismissal for the browser session", async () => {
    await act(async () => { root.render(<SystemWarnings />); await Promise.resolve(); });
    expect(host.textContent).toContain("Development catalogue key is trusted. Don't use this setup in production.");
    expect(host.textContent).toContain("Outside integrations can be installed without confirmation.");
    expect(host.textContent).toContain("Details");
    expect(host.textContent).not.toContain("LYNEWRIGHT_");
    expect(host.querySelector('a[href*="security.md"]')).not.toBeNull();
    await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-label="Dismiss message"]')?.click(); });
    expect(host.textContent).toBe("");
    act(() => root.unmount());
    root = createRoot(host);
    await act(async () => { root.render(<SystemWarnings />); await Promise.resolve(); });
    expect(host.textContent).toBe("");
  });

  it("stays hidden when no warning policy is active", async () => {
    vi.mocked(catalogueApi.get).mockResolvedValue({ status: { dev_key_trusted: false } } as never);
    vi.mocked(integrationManagementApi.list).mockResolvedValue({ install_policy: "confirm", items: [], package_statuses: [] } as never);
    await act(async () => { root.render(<SystemWarnings />); await Promise.resolve(); });
    expect(host.textContent).toBe("");
  });
});
