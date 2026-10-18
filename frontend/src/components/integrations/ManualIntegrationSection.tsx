import { useState } from "react";
import { Button, FormField, Input, Select, Textarea } from "../ui";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary, IntegrationInstallConfirmation } from "../../types";
import InstallConfirmationDialog from "./InstallConfirmationDialog";
import { installConfirmationFromError, type PendingInstallConfirmation } from "./installConfirmation";

type Props = { onCreated: (integration: IntegrationManagementSummary) => void; onClose?: () => void; reportError: (message: string) => void };
type RuntimeKind = "mcp" | "openapi" | "webhook";

export default function ManualIntegrationSection({ onCreated, onClose, reportError }: Props) {
  const [name, setName] = useState("");
  const [runtime, setRuntime] = useState<RuntimeKind>("mcp");
  const [endpoint, setEndpoint] = useState("");
  const [document, setDocument] = useState("");
  const [credentialRefs, setCredentialRefs] = useState("");
  const [eventType, setEventType] = useState("");
  const [payloadMapping, setPayloadMapping] = useState("");
  const [verificationMode, setVerificationMode] = useState("hmac_sha256");
  const [signatureHeader, setSignatureHeader] = useState("x-signature");
  const [timestampHeader, setTimestampHeader] = useState("");
  const [deliveryHeader, setDeliveryHeader] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmation, setConfirmation] = useState<PendingInstallConfirmation | null>(null);

  async function save(confirm?: IntegrationInstallConfirmation) {
    let mapped: Record<string, string> | undefined;
    try {
      mapped = runtime === "webhook" && payloadMapping.trim()
        ? Object.fromEntries(payloadMapping.split(",").map((entry) => {
          const [target, ...source] = entry.split("=");
          if (!target?.trim() || !source.join("=").trim()) throw new Error("Payload mapping entries must use target=source format.");
          return [target.trim(), source.join("=").trim()];
        }))
        : undefined;
    } catch (error) { reportError(error instanceof Error ? error.message : "Payload mapping is invalid."); return; }
    setSaving(true); reportError("");
    try {
      const result = await integrationManagementApi.createManual({
        name: name.trim(), runtime_kind: runtime, endpoint_url: endpoint.trim(),
        ...(runtime === "openapi" && document.trim() ? { document: document.trim() } : {}),
        credential_refs: credentialRefs.split(",").map((value) => value.trim()).filter(Boolean),
        ...(runtime === "webhook" ? {
          event_type: eventType.trim(), payload_mapping: mapped,
          verification: { mode: verificationMode, credential_ref: credentialRefs.split(",")[0]?.trim() ?? "", signature_header: signatureHeader, timestamp_header: timestampHeader, delivery_header: deliveryHeader, tolerance_seconds: 300 },
        } : {}),
      }, confirm);
      onCreated(result); onClose?.(); setConfirmation(null);
    } catch (error) {
      const pending = installConfirmationFromError(error);
      if (pending) setConfirmation(pending);
      else reportError(error instanceof Error ? error.message : "Manual integration could not be saved.");
    } finally { setSaving(false); }
  }

  return <div className="u3-manual-form">
    <p className="muted-text">Manual runtimes use declared data. They cannot load or execute user-supplied code. Secrets belong in stored credential references.</p>
    <form onSubmit={(event) => { event.preventDefault(); setConfirmation(null); void save(); }}>
      <FormField label="Name" htmlFor="manual-integration-name"><Input id="manual-integration-name" required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></FormField>
      <FormField label="Kind" htmlFor="manual-integration-runtime"><Select id="manual-integration-runtime" value={runtime} onChange={(event) => setRuntime(event.target.value as RuntimeKind)}><option value="mcp">MCP</option><option value="openapi">OpenAPI</option><option value="webhook">Webhook</option></Select></FormField>
      <FormField label="Endpoint" htmlFor="manual-integration-endpoint" helpText="Use a public HTTPS endpoint or a configured internal endpoint reference in Connections."><Input id="manual-integration-endpoint" type="url" required value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://…" /></FormField>
      <FormField label="Credential references" htmlFor="manual-integration-credentials" helpText="Enter names of stored credentials, not secret values."><Input id="manual-integration-credentials" value={credentialRefs} onChange={(event) => setCredentialRefs(event.target.value)} /></FormField>
      {runtime === "openapi" ? <FormField label="OpenAPI document" htmlFor="manual-openapi-document" helpText="Validation happens on the server. Saving does not approve operations."><Textarea id="manual-openapi-document" rows={8} value={document} onChange={(event) => setDocument(event.target.value)} /></FormField> : null}
      {runtime === "webhook" ? <>
        <FormField label="Event type" htmlFor="manual-webhook-event"><Input id="manual-webhook-event" required value={eventType} onChange={(event) => setEventType(event.target.value)} /></FormField>
        <FormField label="Verification" htmlFor="manual-webhook-verification"><Select id="manual-webhook-verification" value={verificationMode} onChange={(event) => setVerificationMode(event.target.value)}><option value="hmac_sha256">HMAC-SHA256</option><option value="shared_secret">Shared secret</option></Select></FormField>
        <FormField label="Signature header" htmlFor="manual-webhook-signature"><Input id="manual-webhook-signature" required value={signatureHeader} onChange={(event) => setSignatureHeader(event.target.value)} /></FormField>
        <FormField label="Timestamp header" htmlFor="manual-webhook-timestamp"><Input id="manual-webhook-timestamp" value={timestampHeader} onChange={(event) => setTimestampHeader(event.target.value)} /></FormField>
        <FormField label="Delivery header" htmlFor="manual-webhook-delivery"><Input id="manual-webhook-delivery" value={deliveryHeader} onChange={(event) => setDeliveryHeader(event.target.value)} /></FormField>
        <FormField label="Payload mapping (optional)" htmlFor="manual-webhook-mapping" helpText="Map host event fields as target=source pairs, separated by commas."><Input id="manual-webhook-mapping" value={payloadMapping} onChange={(event) => setPayloadMapping(event.target.value)} /></FormField>
      </> : null}
      <div className="button-row"><Button type="submit" busy={saving}>Save manual integration</Button><Button variant="secondary" disabled={saving} onClick={onClose}>Cancel</Button></div>
    </form>
    {confirmation ? <InstallConfirmationDialog summary={confirmation.summary} summaryHash={confirmation.summaryHash} busy={saving} onConfirm={(value) => void save(value)} onCancel={() => setConfirmation(null)} /> : null}
  </div>;
}
