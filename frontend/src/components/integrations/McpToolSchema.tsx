import { Button } from "../ui";
import { useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMcpToolSchemaResult } from "../../types";
import type { Mutate } from "./sectionTypes";

type Props = {
  integrationId: string;
  connectionId: string;
  toolName: string;
  busy: boolean;
  mutate: Mutate;
};

export default function McpToolSchema({ integrationId, connectionId, toolName, busy, mutate }: Props) {
  const [result, setResult] = useState<IntegrationMcpToolSchemaResult | null>(null);

  async function inspect() {
    const response = await mutate(
      `mcp-schema:${toolName}`,
      () => integrationManagementApi.mcpToolInputSchema(integrationId, connectionId, toolName),
      "Live MCP input schema loaded without persisting it.",
    );
    if (response) setResult(response);
  }

  return <details className="settings-advanced mcp-tool-probe">
    <summary><strong>Input fields</strong><span>Read the live schema; nothing is executed or saved.</span></summary>
    <Button variant="secondary" type="button" disabled={busy} onClick={() => void inspect()}>
      Load schema
    </Button>
    {result ? <div className={`callout${result.truncated ? " callout--warning" : ""}`} role="status">
      <strong>{result.truncated ? "Schema exceeds the display bound" : "Live input schema"}</strong>
      <p className="muted-text">{result.serialized_bytes.toLocaleString()} serialized bytes{result.truncated ? "; only a redacted bounded prefix is shown" : ""}.</p>
      <pre className="json-preview">{JSON.stringify(result.input_schema, null, 2)}</pre>
    </div> : null}
  </details>;
}
