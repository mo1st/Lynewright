import { Button } from "../ui";
import { useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMcpTool, IntegrationMcpToolProbeResult } from "../../types";
import type { Mutate } from "./sectionTypes";

type Props = {
  integrationId: string;
  connectionId: string;
  tool: IntegrationMcpTool;
  busy: boolean;
  mutate: Mutate;
  reportError: (message: string) => void;
};

function scalarLeaves(value: unknown, path = "result"): Array<{ path: string; value: string }> {
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
    return [{ path, value: String(value) }];
  }
  if (Array.isArray(value)) return value.flatMap((item, index) => scalarLeaves(item, `${path}.${index}`));
  if (value && typeof value === "object") {
    return Object.entries(value).flatMap(([key, item]) => scalarLeaves(item, `${path}.${key}`));
  }
  return [];
}

function formatted(value: unknown) {
  return JSON.stringify(value, null, 2) ?? "null";
}

export default function McpToolProbe({ integrationId, connectionId, tool, busy, mutate, reportError }: Props) {
  const [argumentsText, setArgumentsText] = useState("{}");
  const [result, setResult] = useState<IntegrationMcpToolProbeResult | null>(null);
  const [copied, setCopied] = useState("");
  const canProbe = tool.approved && tool.read_only === true && tool.destructive === false && tool.safety_known === true;

  async function probe() {
    let arguments_: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(argumentsText);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Probe arguments must be a JSON object.");
      arguments_ = parsed as Record<string, unknown>;
    } catch (error) {
      reportError(error instanceof Error ? error.message : "Probe arguments must be valid JSON.");
      return;
    }
    const response = await mutate(
      `mcp-probe:${tool.name}`,
      () => integrationManagementApi.probeMcpTool(integrationId, connectionId, tool.name, arguments_),
      "Read-only MCP probe completed.",
    );
    if (response) setResult(response);
  }

  async function copy(path: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(path);
      window.setTimeout(() => setCopied((current) => current === path ? "" : current), 1_500);
    } catch {
      reportError("The browser did not allow copying this value.");
    }
  }

  if (!tool.approved) return null;
  if (!canProbe) return <span className="muted-text">Probe unavailable: the tool is not explicitly read-only and non-destructive.</span>;

  const leaves = result ? scalarLeaves(result.result) : [];
  return <details className="settings-advanced mcp-tool-probe">
    <summary><strong>Probe read-only tool</strong><span>Invoke once with bounded output; no capability is granted.</span></summary>
    <label htmlFor={`mcp-probe-arguments-${tool.name}`}>Arguments (JSON object)
      <textarea id={`mcp-probe-arguments-${tool.name}`} rows={3} value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} />
    </label>
    <Button variant="secondary" type="button" disabled={busy} onClick={() => void probe()}>Probe {tool.name}</Button>
    {result ? <div className="callout" role="status">
      <strong>Probe result</strong>
      <p className="muted-text">{result.truncated ? `Response truncated for display (${result.serialized_bytes.toLocaleString()} serialized bytes).` : `${result.serialized_bytes.toLocaleString()} serialized bytes.`}</p>
      <pre className="json-preview">{formatted(result.result)}</pre>
      {leaves.length ? <div className="mcp-tool-probe__leaves"><strong>Copy scalar values</strong>{leaves.slice(0, 100).map((leaf) => <div className="mcp-tool-probe__leaf" key={leaf.path}><code>{leaf.path}</code><span>{leaf.value}</span><Button variant="secondary" type="button" onClick={() => void copy(leaf.path, leaf.value)}>{copied === leaf.path ? "Copied" : "Copy"}</Button></div>)}</div> : null}
    </div> : null}
  </details>;
}
