import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { IntegrationInstallConfirmation, IntegrationInstallSummary } from "../../types";

type Props = {
  summary: IntegrationInstallSummary;
  summaryHash: string;
  busy?: boolean;
  onConfirm: (confirmation: IntegrationInstallConfirmation) => void;
  onCancel: () => void;
};

export default function InstallConfirmationDialog({ summary, summaryHash, busy = false, onConfirm, onCancel }: Props) {
  const [typedId, setTypedId] = useState("");
  useEffect(() => setTypedId(""), [summaryHash]);
  const matches = typedId.trim() === summary.integration_id;
  const strongest = summary.execution.includes("INSIDE Lynewright");
  return <div className="install-confirmation-backdrop">
    <section className={`install-confirmation${strongest ? " install-confirmation--strong" : ""}`} role="dialog" aria-modal="true" aria-labelledby="install-confirmation-title">
      <header><h2 id="install-confirmation-title">Confirm outside integration</h2></header>
      {strongest ? <div className="callout callout--danger" role="alert"><strong>Runs inside Lynewright with access to all data</strong><p>This package can access data available to the Lynewright process.</p></div> : null}
      <dl className="install-confirmation__summary">
        <dt>Integration</dt><dd>{summary.name} ({summary.integration_id})</dd>
        <dt>Version</dt><dd>{summary.version || "unspecified"}</dd>
        <dt>Kind</dt><dd>{summary.kind}</dd>
        <dt>Source</dt><dd>{summary.source}</dd>
        {summary.declared_source ? <><dt>Declared endpoint</dt><dd>{summary.declared_source}</dd></> : null}
        {summary.sha256 ? <><dt>SHA-256</dt><dd><code>{summary.sha256}</code></dd></> : null}
        <dt>Execution</dt><dd>{summary.execution}</dd>
        <dt>Capabilities</dt><dd>{summary.requested_capabilities.length ? summary.requested_capabilities.join(", ") : "None declared"}</dd>
        <dt>Egress</dt><dd>{String(summary.egress)}</dd>
        <dt>Credential refs</dt><dd>{summary.credential_refs.length ? summary.credential_refs.join(", ") : "None"}</dd>
        <dt>Media access</dt><dd>{Array.isArray(summary.media_access) ? summary.media_access.join(", ") || "None declared" : String(summary.media_access ?? "None declared")}</dd>
        <dt>Catalogue</dt><dd>{summary.catalogue_status}</dd>
      </dl>
      <label htmlFor="install-confirmation-id">Type <code>{summary.integration_id}</code> to confirm<input id="install-confirmation-id" autoComplete="off" value={typedId} onChange={(event) => setTypedId(event.target.value)} /></label>
      <div className="button-row">
        <Button variant="primary" type="button" disabled={!matches || busy} onClick={() => onConfirm({ integration_id: summary.integration_id, summary_hash: summaryHash })}>{busy ? "Installing…" : "Confirm install"}</Button>
        <Button variant="secondary" type="button" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </section>
  </div>;
}
