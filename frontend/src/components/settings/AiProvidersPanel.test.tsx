/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AiProviderProfile, AiProviderTestResponse, AiSettingsRead } from "../../types";

const apiMock = vi.hoisted(() => ({ getAiSettings: vi.fn() }));
vi.mock("../../api/client", () => ({ api: apiMock }));
vi.mock("../../context/ConfirmContext", () => ({ confirmAction: async () => true }));
const { AiProvidersPanel } = await import("./AiProvidersPanel");

const profile: AiProviderProfile = {
  id: 12, name: "Primary route", provider: "openai_compatible", protocol: "openai_chat", model: "gpt-test",
  base_url: "https://example.test/v1", credential_ref: "ai.primary", priority: 2, enabled: true,
  api_key_set: true, api_key_masked: "••••", updated_at: "2026-10-17T10:00:00Z",
};
const settings: AiSettingsRead = {
  provider: "mock", model: "mock-brief-v1", base_url: "", api_keys: {}, api_key_set: false,
  updated_at: "2026-10-17T10:00:00Z", storage_warning: "Protected", providers: [profile],
};

describe("AI provider settings", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.appendChild(host);
    apiMock.getAiSettings.mockResolvedValue(settings);
  });

  afterEach(() => { act(() => root?.unmount()); host.remove(); apiMock.getAiSettings.mockReset(); });

  it("shows configured routes in priority order and a fixed deterministic fallback row", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<AiProvidersPanel busy={false} onTest={async () => ({} as AiProviderTestResponse)} savedSettings={settings} testResult={null} openDrawers={{}} onToggleDrawer={() => undefined} />);
    });
    expect(host.querySelector('table[aria-label="AI provider priority"]')).not.toBeNull();
    expect(host.textContent).toContain("Primary route");
    expect(host.textContent).toContain("Deterministic fallback");
    expect(host.textContent).toContain("Not tested this session");
    expect(host.querySelector('[data-button-variant="danger"]')?.textContent).toContain("Disable");
  });

  it("opens Add provider in the shared dialog with labelled fields", async () => {
    await act(async () => {
      root = createRoot(host);
      root.render(<AiProvidersPanel busy={false} onTest={async () => ({} as AiProviderTestResponse)} savedSettings={settings} testResult={null} openDrawers={{}} onToggleDrawer={() => undefined} />);
    });
    await act(async () => { host.querySelector<HTMLButtonElement>('button[data-button-variant="primary"]')?.click(); });
    expect(host.querySelector('[role="dialog"][aria-modal="true"]')).not.toBeNull();
    expect(host.textContent).toContain("Use a credential reference for saved secrets.");
    expect(host.querySelector('label[for="ai-profile-name"]')).not.toBeNull();
    expect(host.querySelector('label[for="ai-provider"]')).not.toBeNull();
  });
});
