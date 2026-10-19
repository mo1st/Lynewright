/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

vi.mock("../../api/integrationManagementClient", () => ({
  integrationManagementApi: { mcpToolInputSchema: vi.fn() },
}));

const { default: McpToolSchema } = await import("./McpToolSchema");

describe("McpToolSchema", () => {
  let host: HTMLDivElement;
  let root: Root;
  const mutate: Mutate = async (_key, action) => action();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("loads and labels the live non-persistent schema", async () => {
    vi.mocked(integrationManagementApi.mcpToolInputSchema).mockResolvedValue({
      integration_id: "fixture", connection_id: "primary", tool_name: "create",
      input_schema: { type: "object", required: ["channelId"] }, truncated: false, serialized_bytes: 52,
    });
    await act(async () => {
      root = createRoot(host);
      root.render(<McpToolSchema integrationId="fixture" connectionId="primary" toolName="create" busy={false} mutate={mutate} />);
      await Promise.resolve();
    });
    const load = Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("Load schema"));
    await act(async () => { load?.click(); await Promise.resolve(); });
    expect(integrationManagementApi.mcpToolInputSchema).toHaveBeenCalledWith("fixture", "primary", "create");
    expect(host.textContent).toContain("nothing is executed or saved");
    expect(host.textContent).toContain("channelId");
  });
});
