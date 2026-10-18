import { beforeEach, describe, expect, it, vi } from "vitest";

const apiRequest = vi.fn();
vi.mock("./client", () => ({ apiRequest: (...args: unknown[]) => apiRequest(...args) }));

const { integrationManagementApi } = await import("./integrationManagementClient");

function lastCall(): { path: string; options?: RequestInit } {
  const calls = apiRequest.mock.calls;
  const call = calls[calls.length - 1];
  if (!call) throw new Error("apiRequest was never called");
  return { path: call[0] as string, options: call[1] as RequestInit | undefined };
}

beforeEach(() => apiRequest.mockReset());

describe("integration management connection contracts", () => {
  it("keeps OpenAPI operation visibility scoped to an explicit connection", async () => {
    await integrationManagementApi.openApiOperations("integration/a", "staging");
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/openapi/operations?connection_id=staging");
  });

  it("uses the connection lifecycle routes without weakening the origin contract", async () => {
    await integrationManagementApi.createConnection("integration", {
      name: "Internal",
      origin_kind: "internal_endpoint",
      internal_endpoint_ref: "internal-api-test",
      endpoint_url: "",
    });
    expect(lastCall().path).toBe("/api/integration-management/integration/connections");
    expect(lastCall().options?.method).toBe("POST");
    expect(JSON.parse(String(lastCall().options?.body))).toMatchObject({ origin_kind: "internal_endpoint", internal_endpoint_ref: "internal-api-test", endpoint_url: "" });

    await integrationManagementApi.testConnection("integration", "staging");
    expect(lastCall().path).toBe("/api/integration-management/integration/connections/staging/test");

    await integrationManagementApi.disableConnection("integration", "staging");
    expect(lastCall().path).toBe("/api/integration-management/integration/connections/staging/disable");
  });

  it("submits an OAuth callback only to the originating connection", async () => {
    await integrationManagementApi.oauthCallback("integration/a", "staging", {
      state: "provider-state",
      code: "provider-code",
      access_credential_ref: "oauth.integration-a.staging.access",
      refresh_credential_ref: "oauth.integration-a.staging.refresh",
    });
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/connections/staging/oauth/callback");
    expect(JSON.parse(String(lastCall().options?.body))).toMatchObject({
      state: "provider-state", code: "provider-code", access_credential_ref: "oauth.integration-a.staging.access",
    });
  });

  it("previews and removes integrations through the shared removal endpoints", async () => {
    await integrationManagementApi.removalPreview("integration/a");
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/removal-preview");
    expect(lastCall().options?.method).toBeUndefined();
    await integrationManagementApi.remove("integration/a", {
      confirm_name: "integration/a", disable_using_lines: true, delete_exclusive_credentials: false,
    });
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa");
    expect(lastCall().options?.method).toBe("DELETE");
    expect(JSON.parse(String(lastCall().options?.body))).toEqual({
      confirm_name: "integration/a", disable_using_lines: true, delete_exclusive_credentials: false,
    });
  });

  it("keeps MCP contract mappings scoped to the selected connection", async () => {
    await integrationManagementApi.createMcpContractMapping("integration/a", "staging", {
      tool_name: "lookup", target_contract: "lynewright.fixture.read", target_operation: "fetch",
      argument_mapping: { query: "query" }, output_mapping: { value: "answer" }, enabled: true,
    });
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/connections/staging/mcp/contract-mappings");
  });

  it("revises and removes MCP contract mappings on the same selected connection", async () => {
    const payload = {
      tool_name: "lookup", target_contract: "lynewright.fixture.read", target_operation: "fetch",
      argument_mapping: { query: "query" }, output_mapping: { value: "answer" }, enabled: true,
    };
    await integrationManagementApi.updateMcpContractMapping("integration/a", "staging", "mapping/1", payload);
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/connections/staging/mcp/contract-mappings/mapping%2F1");
    expect(lastCall().options?.method).toBe("PATCH");
    await integrationManagementApi.deleteMcpContractMapping("integration/a", "staging", "mapping/1");
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/connections/staging/mcp/contract-mappings/mapping%2F1");
    expect(lastCall().options?.method).toBe("DELETE");
  });

  it("forwards an OpenAPI YAML document unchanged for backend validation", async () => {
    const yaml = "openapi: 3.0.0\npaths: {}";
    await integrationManagementApi.createManual({ name: "YAML API", runtime_kind: "openapi", endpoint_url: "https://api.example.test", document: yaml });
    expect(JSON.parse(String(lastCall().options?.body))).toMatchObject({ document: yaml });
  });

  it("creates generic OpenAPI mappings without a provider-specific field", async () => {
    await integrationManagementApi.createOpenApiResourceMapping("integration", {
      resource_type: "universe_object",
      label: "Universe object",
      plural_label: "Universe objects",
      list_operation_id: "listObjects",
      get_operation_id: null,
      create_operation_id: null,
      update_operation_id: null,
      delete_operation_id: null,
      get_external_id_target: null,
      update_external_id_target: null,
      delete_external_id_target: null,
      items_path: "data.items",
      id_path: "id",
      name_path: "name",
      parent_id_path: null,
      url_path: null,
      status_path: null,
      display_fields: [],
      metadata_paths: {},
      pagination: null,
      enabled: true,
    });
    expect(lastCall().path).toBe("/api/integration-management/integration/openapi/resource-mappings");
    expect(JSON.parse(String(lastCall().options?.body))).toMatchObject({ resource_type: "universe_object", list_operation_id: "listObjects" });
  });

  it("keeps MCP resource mappings scoped by connection and preserves the mapping route shape", async () => {
    await integrationManagementApi.previewMcpResourceMapping("integration/a", "staging", {
      resource_type: "social_channel", label: "Social channel", plural_label: "Social channels",
      list_tool_name: "listChannels", list_arguments: { organization: "org-1" },
      get_tool_name: null, create_tool_name: null, update_tool_name: null, delete_tool_name: null,
      get_external_id_target: null, update_external_id_target: null, delete_external_id_target: null,
      items_path: "data.items", id_path: "id", name_path: "name", parent_id_path: null,
      url_path: null, status_path: null, display_fields: [], metadata_paths: {}, enabled: true,
    });
    expect(lastCall().path).toBe("/api/integration-management/integration%2Fa/mcp/resource-mappings/preview?connection_id=staging");
    expect(lastCall().options?.method).toBe("POST");
    expect(JSON.parse(String(lastCall().options?.body))).toMatchObject({ list_tool_name: "listChannels", list_arguments: { organization: "org-1" } });
  });
});
