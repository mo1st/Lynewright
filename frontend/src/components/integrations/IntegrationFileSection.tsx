import { useState, type ChangeEvent } from "react";
import { ApiError, describeApiError } from "../../api/client";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import type { IntegrationInstallConfirmation } from "../../types";
import InstallConfirmationDialog from "./InstallConfirmationDialog";
import { installConfirmationFromError, type PendingInstallConfirmation } from "./installConfirmation";
import { Button, FormField, Input, Textarea } from "../ui";

type Props = {
  selected: IntegrationManagementSummary | null;
  onImported: (integration: IntegrationManagementSummary) => void;
  embedded?: boolean;
  onClose?: () => void;
};

function validationErrors(error: unknown): string[] {
  if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
    const details = (error.detail as { errors?: unknown }).errors;
    if (Array.isArray(details)) return details.map((item) => String(item));
  }
  return [describeApiError(error, "The integration file could not be imported.")];
}

export default function IntegrationFileSection({ selected, onImported, embedded = false, onClose }: Props) {
  const [open, setOpen] = useState(embedded);
  const [file, setFile] = useState<File | null>(null);
  const [pastedText, setPastedText] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState<PendingInstallConfirmation | null>(null);

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    setFile(event.target.files?.[0] ?? null);
    setPastedText("");
    setErrors([]);
    setNotice("");
  }

  async function importFile(confirm?: IntegrationInstallConfirmation) {
    const selectedFile = file ?? (pastedText.trim() ? new File([pastedText], "integration.yaml", { type: "application/yaml" }) : null);
    if (!selectedFile) return;
    setBusy(true);
    setErrors([]);
    setNotice("");
    try {
      const result = confirm
        ? await integrationManagementApi.importFile(selectedFile, confirm)
        : await integrationManagementApi.importFile(selectedFile);
      onImported(result.integration);
      setNotice(result.approval_message || (result.idempotent ? "This integration file was already imported." : "Integration file imported."));
      setFile(null);
      setPastedText("");
      setOpen(false);
      onClose?.();
      setConfirmation(null);
    } catch (error) {
      const pending = installConfirmationFromError(error);
      if (pending) {
        setConfirmation(pending);
        setErrors([]);
      } else {
        setErrors(validationErrors(error));
      }
    } finally {
      setBusy(false);
    }
  }

  async function exportFile() {
    if (!selected) return;
    setBusy(true);
    setErrors([]);
    setNotice("");
    try {
      const content = await integrationManagementApi.exportFile(selected.integration.id);
      const url = URL.createObjectURL(new Blob([content], { type: "application/yaml;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${selected.integration.id}.yaml`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      setNotice("Integration file exported.");
    } catch (error) {
      setErrors(validationErrors(error));
    } finally {
      setBusy(false);
    }
  }

  return <section className={`panel integration-file-panel${embedded ? " u3-flow-embedded" : ""}`} aria-label="Integration file">
    {!embedded ? <header className="section-heading">
      <div><h3>Integration file</h3><p>Share data-only integrations as YAML or JSON.</p></div>
      <div className="button-row">
        <Button variant="secondary" disabled={busy} onClick={() => { setOpen((current) => !current); setErrors([]); }}>
          Import file
        </Button>
        {selected?.availability.declarative ? <Button variant="secondary" disabled={busy} onClick={() => void exportFile()}>
          {busy ? "Working…" : "Export"}
        </Button> : null}
      </div>
    </header> : null}
    {open ? <div className="form-grid" data-testid="integration-file-import">
      <FormField label="Choose an integration file" htmlFor="integration-file-upload"><Input id="integration-file-upload" type="file" accept=".yaml,.yml,.json,application/yaml,application/json" onChange={chooseFile} /></FormField>
      <FormField label="Or paste YAML / JSON" htmlFor="integration-file-paste"><Textarea id="integration-file-paste" rows={7} value={pastedText} onChange={(event) => { setPastedText(event.target.value); setFile(null); setErrors([]); }} /></FormField>
      {file ? <p className="muted-text">Selected: {file.name}</p> : null}
      <div className="button-row">
        <Button disabled={(!file && !pastedText.trim()) || busy} busy={busy} onClick={() => void importFile()}>
          {busy ? "Importing…" : "Import selected file"}
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => { setOpen(false); setFile(null); setPastedText(""); setErrors([]); onClose?.(); }}>
          Cancel
        </Button>
      </div>
    </div> : null}
    {errors.length ? <ul className="form-error" role="alert" data-testid="integration-file-errors">
      {errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}
    </ul> : null}
    {notice ? <p className="form-success" role="status">{notice}</p> : null}
    {confirmation ? <InstallConfirmationDialog
      summary={confirmation.summary}
      summaryHash={confirmation.summaryHash}
      busy={busy}
      onConfirm={(confirm) => void importFile(confirm)}
      onCancel={() => setConfirmation(null)}
    /> : null}
  </section>;
}
