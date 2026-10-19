import { Button } from "../ui";
import { useCallback, useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

type Identity = {
  integration_id?: string;
  definition_version?: string;
  manifest_fingerprint?: string;
  contract_versions?: Record<string, string>;
};

type DiffEntry = { from: unknown; to: unknown };

type Preview = {
  mode?: string;
  current?: Identity;
  candidate?: Identity;
  diff?: Record<string, DiffEntry>;
  rollback?: string;
  conformance?: Record<string, unknown>;
  dry_run?: Record<string, unknown>;
  capability_change?: {
    known: boolean;
    changed: boolean;
    added?: Record<string, string[]>;
    removed?: Record<string, string[]>;
  };
};

type Props = {
  integrationId: string;
  /** The activation lock and the deployed definition disagree. */
  runtimeUnavailable: boolean;
  busy: boolean;
  mutate: Mutate;
};

const RECONCILE = "deployed_reconcile";

/** Fingerprints are 64 hex characters; show enough to compare by eye. */
const shortFingerprint = (value: unknown): string => {
  const text = String(value ?? "");
  return text.length > 16 ? `${text.slice(0, 12)}…` : text || "—";
};

const describe = (field: string, value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (field === "manifest_fingerprint") return shortFingerprint(value);
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key} ${String(item)}`)
      .join(", ") || "—";
  }
  return String(value);
};

const FIELD_LABELS: Record<string, string> = {
  definition_version: "Definition version",
  manifest_fingerprint: "Manifest fingerprint",
  contract_versions: "Contract versions",
};

export default function DefinitionUpgradeSection({ integrationId, runtimeUnavailable, busy, mutate }: Props) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const changes = Object.entries(preview?.diff ?? {});
  const isReconcile = preview?.mode === RECONCILE;
  const candidateVersion = String(preview?.candidate?.definition_version ?? "");
  const candidateFingerprint = String(preview?.candidate?.manifest_fingerprint ?? "");
  const applicable = Boolean(preview) && changes.length > 0 && Boolean(candidateVersion) && Boolean(candidateFingerprint);

  const runPreview = useCallback(async () => {
    setLoading(true);
    setFailure("");
    setConfirmed(false);
    try {
      const result = await integrationManagementApi.definitionUpgradePreview(integrationId);
      setPreview(result as Preview);
    } catch (error) {
      setPreview(null);
      setFailure(error instanceof Error ? error.message : "The upgrade preview could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [integrationId]);

  useEffect(() => {
    if (runtimeUnavailable) void runPreview();
  }, [runtimeUnavailable, runPreview]);

  const runApply = async () => {
    if (!applicable) return;
    const applied = await mutate(
      "definition-upgrade",
      () => integrationManagementApi.definitionUpgradeApply(integrationId, {
        expected_definition_version: candidateVersion,
        expected_manifest_fingerprint: candidateFingerprint,
      }),
      "Activation lock moved to the deployed definition.",
    );
    if (applied) {
      setPreview(null);
      setConfirmed(false);
    }
  };

  return <section className="integration-subsection" aria-label="Definition upgrade">
    <header className="panel-header"><div><h4>Deployed definition</h4>
      <p className="muted-text">
        An activated integration is locked to one exact definition. When a release changes the
        manifest, the lock still names the previous one and the integration cannot activate until
        the lock is moved. Preview reads only; nothing changes until you apply.
      </p>
    </div></header>

    {runtimeUnavailable && !preview ? <p className="muted-text">
      This integration is enabled but reporting an error, which is what a stale activation lock
      looks like. Preview the deployed definition to see whether that is the cause.
    </p> : null}

    {runtimeUnavailable && applicable ? <p className="form-success" role="status">
      <strong>Upgrade available.</strong> Review the deployed definition below before applying it.
    </p> : null}

    <div className="button-row">
      <Button variant="secondary" type="button" disabled={busy || loading} onClick={() => void runPreview()}>
        {loading ? "Checking…" : "Preview deployed definition"}
      </Button>
    </div>

    {failure ? <p className="form-error" role="alert">{failure}</p> : null}

    {preview && changes.length === 0 ? <p className="form-success" role="status">
      The activation lock already names the deployed definition. Nothing to apply.
    </p> : null}

    {preview && changes.length > 0 ? <>
      <div className="table-wrap integration-table-wrap">
        <table className="responsive-record-table integration-record-table">
          <caption className="muted-text">What applying would change</caption>
          <thead><tr><th scope="col">Field</th><th scope="col">Activated</th><th scope="col">Deployed</th></tr></thead>
          <tbody>
            {changes.map(([field, entry]) => <tr key={field}>
              <td data-label="Field">{FIELD_LABELS[field] ?? field}</td>
              <td data-label="Activated" className="mono-text" title={field === "manifest_fingerprint" ? String(entry.from ?? "") : undefined}>{describe(field, entry.from)}</td>
              <td data-label="Deployed" className="mono-text" title={field === "manifest_fingerprint" ? String(entry.to ?? "") : undefined}>{describe(field, entry.to)}</td>
            </tr>)}
          </tbody>
        </table>
      </div>

      {preview.capability_change ? <p className="muted-text" role="status">
        {preview.capability_change.known
          ? preview.capability_change.changed
            ? "The deployed definition changes declared capabilities and needs review."
            : "No declared capability changes were found."
          : "Capability parity could not be confirmed; review is required."}
      </p> : null}

      {isReconcile ? <section className="callout callout--warning" aria-label="Rollback is a redeploy">
        <strong>This cannot be undone from here</strong>
        <p>
          The previously activated definition is not present in this release, so there is nothing to
          restore in place. Rolling back means deploying the previous release again.
        </p>
        <p className="muted-text">
          If the new definition fails to activate, the previous activation lock is restored
          automatically so this integration is left exactly as it is now.
        </p>
      </section> : <p className="muted-text">
        Both definitions are present, so a failed activation is rolled back in place.
      </p>}

      <label className="checkbox-row" htmlFor="definition-upgrade-confirm">
        <input
          id="definition-upgrade-confirm"
          name="definition-upgrade-confirm"
          autoComplete="off"
          type="checkbox"
          checked={confirmed}
          disabled={busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        {" "}I reviewed this change{isReconcile ? " and accept that rollback requires redeploying the previous release" : ""}.
      </label>

      <div className="button-row">
        <Button variant="secondary" type="button" disabled={busy || !confirmed} onClick={() => void runApply()}>
          Apply deployed definition
        </Button>
      </div>
      <p className="muted-text">
        Applies version {candidateVersion} · fingerprint {shortFingerprint(candidateFingerprint)}. If the
        deployment changed since this preview, the request is refused rather than retargeted.
      </p>
    </> : null}
  </section>;
}
