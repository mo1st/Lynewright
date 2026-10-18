import { useEffect, useMemo, useState, type FormEvent } from "react";
import { describeApiError } from "../../api/client";
import { integrationManagementApi, type IntegrationDraft } from "../../api/integrationManagementClient";
import { confirmAction } from "../../context/ConfirmContext";
import { useProject } from "../../context/ProjectContext";
import type { IntegrationInstallConfirmation } from "../../types";
import InstallConfirmationDialog from "./InstallConfirmationDialog";
import { installConfirmationFromError, type PendingInstallConfirmation } from "./installConfirmation";
import { Button, FormField, Input, Select, Switch, Textarea } from "../ui";

type SourceKind = IntegrationDraft["source_kind"];
type OperationDraft = { operation_id?: string; tool_name?: string; description?: string; enabled?: boolean };
const SOURCE_LABELS: Record<SourceKind, string> = {
  github_repo: "GitHub repository", openapi_url: "OpenAPI URL",
  mcp_endpoint: "MCP endpoint", pasted_docs: "Pasted API docs",
};
const jsonText = (value: unknown) => JSON.stringify(value, null, 2);

export default function IntegrationDraftSection({ embedded = false, onClose, onEnabled }: { embedded?: boolean; onClose?: () => void; onEnabled?: (integrationId: string) => void } = {}) {
  const { projectId } = useProject();
  const [sourceKind, setSourceKind] = useState<SourceKind>("github_repo");
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [internalRef, setInternalRef] = useState("");
  const [credentialRefs, setCredentialRefs] = useState("");
  const [items, setItems] = useState<IntegrationDraft[]>([]);
  const [draft, setDraft] = useState<IntegrationDraft | null>(null);
  const [savedDraft, setSavedDraft] = useState<Record<string, unknown> | null>(null);
  const [rawDraft, setRawDraft] = useState("");
  const [previousText, setPreviousText] = useState("");
  const [operationId, setOperationId] = useState("");
  const [argsText, setArgsText] = useState("{}");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState<PendingInstallConfirmation | null>(null);
  const [wizardStep, setWizardStep] = useState<1 | 2 | 3 | 4>(1);

  async function refresh() {
    if (!projectId) return;
    const response = await integrationManagementApi.drafts(projectId);
    setItems(response.items);
    if (draft && response.items.some((item) => item.id === draft.id)) {
      await selectDraft(draft.id);
    } else if (response.items[0]) {
      await selectDraft(response.items[0].id);
    } else {
      setDraft(null); setSavedDraft(null); setRawDraft(""); setPreviousText("");
    }
  }
  useEffect(() => {
    let active = true;
    setDraft(null); setSavedDraft(null); setRawDraft(""); setItems([]);
    if (projectId) integrationManagementApi.drafts(projectId).then((response) => {
      if (!active) return;
      setItems(response.items);
      if (response.items[0]) void selectDraft(response.items[0].id);
    }).catch((caught) => { if (active) setError(describeApiError(caught, "Integration drafts could not be loaded.")); });
    return () => { active = false; };
  }, [projectId]);

  async function selectDraft(id: string) {
    if (!projectId) return;
    setError("");
    try {
      const value = await integrationManagementApi.getDraft(id, projectId);
      setDraft(value); setSavedDraft(value.draft_json); setRawDraft(jsonText(value.draft_json));
      setWizardStep(value.status === "enabled" || value.status === "probed" ? 4 : 2);
      setPreviousText(jsonText(value.draft_json));
      const first = operations(value)[0];
      setOperationId(first ? operationName(first, value) : "");
    } catch (caught) { setError(describeApiError(caught, "Draft could not be loaded.")); }
  }
  function operations(value: IntegrationDraft | null): OperationDraft[] {
    const list = value?.draft_json.operations;
    return Array.isArray(list) ? list.filter((item): item is OperationDraft => Boolean(item && typeof item === "object")) : [];
  }
  function operationName(operation: OperationDraft, value: IntegrationDraft): string {
    return String(value.draft_json.runtime_kind === "openapi" ? operation.operation_id ?? "" : operation.tool_name ?? "");
  }
  const enabledOperations = useMemo(() => operations(draft).filter((operation) => operation.enabled !== false), [draft]);
  const isDirty = Boolean(draft && (
    rawDraft !== jsonText(draft.draft_json) || jsonText(draft.draft_json) !== jsonText(savedDraft)
  ));

  async function createDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return;
    setBusy("create"); setError(""); setNotice("");
    try {
      const created = await integrationManagementApi.createDraft({
        source_kind: sourceKind, project_id: projectId,
        ...(sourceKind === "pasted_docs" ? { text } : { url }),
        internal_endpoint_ref: internalRef,
        credential_refs: credentialRefs.split(",").map((item) => item.trim()).filter(Boolean),
      });
      setItems((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setDraft(created); setSavedDraft(created.draft_json); setRawDraft(jsonText(created.draft_json));
      setWizardStep(2);
      setPreviousText(""); setOperationId(operations(created)[0] ? operationName(operations(created)[0], created) : "");
      setNotice(created.source_message || "Draft created. Review it before probing.");
    } catch (caught) { setError(describeApiError(caught, "Integration draft could not be created.")); }
    finally { setBusy(""); }
  }

  async function saveDraft() {
    if (!draft || !projectId) return;
    setBusy("save"); setError(""); setNotice("");
    try {
      const parsed: unknown = JSON.parse(rawDraft);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("The draft must be a JSON object.");
      setPreviousText(savedDraft ? jsonText(savedDraft) : "");
      const updated = await integrationManagementApi.updateDraft(draft.id, projectId, parsed as Record<string, unknown>);
      setDraft(updated); setSavedDraft(updated.draft_json); setRawDraft(jsonText(updated.draft_json));
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice("Draft saved. Run a new probe before enabling it.");
    } catch (caught) { setError(describeApiError(caught, "Draft JSON is invalid.")); }
    finally { setBusy(""); }
  }

  function toggleOperation(index: number, enabled: boolean) {
    if (!draft) return;
    const next = structuredClone(draft.draft_json);
    const ops = Array.isArray(next.operations) ? next.operations as OperationDraft[] : [];
    if (!ops[index]) return;
    ops[index] = { ...ops[index], enabled };
    setDraft({ ...draft, draft_json: next }); setRawDraft(jsonText(next));
  }

  async function probe() {
    if (!draft || !projectId || !operationId) return;
    setBusy("probe"); setError(""); setNotice("");
    setWizardStep(3);
    try {
      const args: unknown = JSON.parse(argsText);
      if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("Probe arguments must be a JSON object.");
      const updated = await integrationManagementApi.probeDraft(draft.id, projectId, operationId, args as Record<string, unknown>);
      setDraft(updated); setSavedDraft(updated.draft_json); setRawDraft(jsonText(updated.draft_json));
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice("Probe passed. Enable is now available for this exact draft.");
      setWizardStep(4);
    } catch (caught) { setError(describeApiError(caught, "Probe failed.")); }
    finally { setBusy(""); }
  }

  async function enable(confirm?: IntegrationInstallConfirmation) {
    if (!draft || !projectId) return;
    setBusy("enable"); setError(""); setNotice("");
    try {
      const updated = confirm
        ? await integrationManagementApi.enableDraft(draft.id, projectId, confirm)
        : await integrationManagementApi.enableDraft(draft.id, projectId);
      setDraft(updated); setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice("Integration enabled through the K4.1 importer.");
      setConfirmation(null);
      if (updated.status === "enabled" && updated.enabled_integration_id) onEnabled?.(updated.enabled_integration_id);
    } catch (caught) {
      const pending = installConfirmationFromError(caught);
      if (pending) setConfirmation(pending);
      else setError(describeApiError(caught, "Integration could not be enabled."));
    }
    finally { setBusy(""); }
  }

  async function discard() {
    if (!draft || !projectId) return;
    const confirmed = await confirmAction({
      title: "Discard integration draft?",
      message: "This removes the unenabled draft. An enabled integration cannot be discarded here.",
      confirmLabel: "Discard draft",
      tone: "danger",
    });
    if (!confirmed) return;
    setBusy("discard"); setError(""); setNotice("");
    try {
      const updated = await integrationManagementApi.discardDraft(draft.id, projectId);
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
      setDraft(updated); setSavedDraft(updated.draft_json); setRawDraft(jsonText(updated.draft_json));
      setNotice("Draft discarded.");
    } catch (caught) { setError(describeApiError(caught, "Draft could not be discarded.")); }
    finally { setBusy(""); }
  }

  async function downloadCommand() {
    if (!draft || !projectId) return;
    setBusy("download"); setError("");
    try {
      const content = await integrationManagementApi.downloadDraft(draft.id, projectId);
      const objectUrl = URL.createObjectURL(new Blob([content], { type: "application/yaml;charset=utf-8" }));
      const anchor = document.createElement("a"); anchor.href = objectUrl; anchor.download = "command.yaml";
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(objectUrl);
    } catch (caught) { setError(describeApiError(caught, "Command draft could not be downloaded.")); }
    finally { setBusy(""); }
  }

  const credentialNames = Array.isArray(draft?.draft_json.credentials)
    ? (draft?.draft_json.credentials as Array<{ name?: string }>).map((item) => item.name).filter(Boolean) as string[]
    : [];
  const validationErrors = draft?.validation_errors ?? [];
  const canEnable = Boolean(draft && draft.draft_kind === "integration_file" && draft.status === "probed"
    && draft.probe_hash_locked && validationErrors.length === 0 && !isDirty && !busy);

  return <section className={`panel integration-draft-panel${embedded ? " u3-flow-embedded" : ""}`} aria-label="Add from link or docs">
    {embedded ? <ol className="u3-flow-steps" aria-label="Draft setup steps">{["Source", "Review", "Probe one operation", "Enable"].map((label, index) => <li key={label} aria-current={wizardStep === index + 1 ? "step" : undefined}>{index + 1} · {label}</li>)}</ol> : null}
    {!embedded ? <header className="section-heading">
      <div><h3>Add from link or docs</h3><p>Build a declarative draft. Review, select operations, and pass a real probe before enabling.</p></div>
      <Button variant="secondary" disabled={Boolean(busy) || !projectId} onClick={() => void refresh()}>Refresh drafts</Button>
    </header> : null}
    <form className="form-grid form-grid--single" onSubmit={(event) => void createDraft(event)}>
      <FormField label="Source" htmlFor="integration-draft-source"><Select id="integration-draft-source" value={sourceKind} onChange={(event) => setSourceKind(event.target.value as SourceKind)}>
        {Object.entries(SOURCE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </Select></FormField>
      {sourceKind === "pasted_docs" ? <>
        <FormField label="API documentation (up to 200 KiB)" htmlFor="integration-draft-text"><Textarea id="integration-draft-text" value={text} onChange={(event) => setText(event.target.value)} rows={5} maxLength={200_000} /></FormField>
      </> : <>
        <FormField label={`${SOURCE_LABELS[sourceKind]} URL`} htmlFor="integration-draft-url"><Input id="integration-draft-url" value={url} onChange={(event) => setUrl(event.target.value)} type="url" required /></FormField>
      </>}
      <details><summary>Advanced connection options</summary><FormField label="Internal endpoint reference (optional)" htmlFor="integration-draft-internal-ref"><Input id="integration-draft-internal-ref" value={internalRef} onChange={(event) => setInternalRef(event.target.value)} maxLength={64} /></FormField></details>
      {sourceKind === "mcp_endpoint" ? <>
        <FormField label="Credential references (comma separated)" htmlFor="integration-draft-credentials"><Input id="integration-draft-credentials" value={credentialRefs} onChange={(event) => setCredentialRefs(event.target.value)} /></FormField>
      </> : null}
      <Button type="submit" disabled={!projectId || Boolean(busy) || (sourceKind === "pasted_docs" ? !text.trim() : !url.trim())} busy={busy === "create"}>
        {busy === "create" ? "Creating draft…" : "Create draft"}
      </Button>
      {embedded ? <Button variant="secondary" disabled={Boolean(busy)} onClick={onClose}>Cancel</Button> : null}
    </form>
    {items.length ? <FormField label="Saved draft" htmlFor="integration-draft-select"><Select id="integration-draft-select" value={draft?.id ?? ""} onChange={(event) => void selectDraft(event.target.value)}>
      {items.map((item) => <option key={item.id} value={item.id}>{SOURCE_LABELS[item.source_kind]} · {item.status}</option>)}
    </Select></FormField> : null}
    {draft ? <div className="integration-draft-review" data-testid="integration-draft-review">
      <h4>{draft.draft_kind === "cli_command" ? "command.yaml draft" : "lynewright-integration.yaml draft"}</h4>
      {draft.source_message ? <p role="status">{draft.source_message}</p> : null}
      {credentialNames.length ? <p><strong>Credential refs needed:</strong> {credentialNames.join(", ")}</p> : null}
      {operations(draft).length ? <fieldset><legend>Operations (new drafts default off)</legend>
        {operations(draft).map((operation, index) => {
          const name = operationName(operation, draft);
          return <Switch key={String(index) + "-" + name} name={`draft-operation-${index}`} label={<>{name} {operation.description ? <span className="muted-text">— {operation.description}</span> : null}</>} checked={operation.enabled !== false} disabled={Boolean(busy) || draft.status === "enabled"} onChange={(enabled) => toggleOperation(index, enabled)} />;
        })}
      </fieldset> : null}
      <div className="form-grid form-grid--single"><FormField label="Edit draft JSON" htmlFor="integration-draft-editor">
        <Textarea id="integration-draft-editor" aria-label="Edit draft JSON" rows={12} spellCheck={false} value={rawDraft} onChange={(event) => setRawDraft(event.target.value)} />
      </FormField>
        <div className="button-row">
          <Button variant="secondary" busy={busy === "save"} disabled={draft.status === "enabled" || draft.status === "discarded"} onClick={() => void saveDraft()}>Save draft</Button>
          {draft.draft_kind === "cli_command" ? <Button variant="secondary" disabled={Boolean(busy)} onClick={() => void downloadCommand()}>Download command.yaml</Button> : null}
        </div>
      </div>
      <details open><summary>Draft diff</summary><div className="button-row"><section><h5>Previous saved version</h5><pre>{previousText || "(new draft)"}</pre></section><section><h5>Current draft</h5><pre>{rawDraft}</pre></section></div></details>
      {validationErrors.length ? <ul className="form-error" role="alert" data-testid="integration-draft-errors">{validationErrors.map((item, index) => <li key={String(index) + "-" + item}>{item}</li>)}</ul> : null}
      {draft.draft_kind === "integration_file" && draft.status !== "enabled" && draft.status !== "discarded" ? <>
        <div className="form-grid form-grid--single"><FormField label="Operation to probe" htmlFor="integration-draft-operation"><Select id="integration-draft-operation" value={operationId} onChange={(event) => setOperationId(event.target.value)}>
            {enabledOperations.map((operation) => <option key={operationName(operation, draft)} value={operationName(operation, draft)}>{operationName(operation, draft)}</option>)}
          </Select></FormField>
          <FormField label="Probe arguments (JSON)" htmlFor="integration-draft-args"><Textarea id="integration-draft-args" value={argsText} onChange={(event) => setArgsText(event.target.value)} rows={4} /></FormField>
          <Button variant="secondary" busy={busy === "probe"} disabled={!operationId || isDirty || validationErrors.length > 0} onClick={() => void probe()}>Run probe</Button>
        </div>
        <p>Probe status: {draft.status === "probed" && draft.probe_hash_locked ? "Passed for the current draft" : "Required before enable"}</p>
        {Object.keys(draft.probe_result).length ? <pre aria-label="Probe result">{jsonText(draft.probe_result)}</pre> : null}
        <Button aria-label="Enable integration" disabled={!canEnable} busy={busy === "enable"} onClick={() => void enable()}>Enable integration</Button>
      </> : null}
      {draft.status === "enabled" ? <p role="status">Enabled integration: {draft.enabled_integration_id}</p> : null}
    </div> : null}
    {error ? <p className="form-error" role="alert">{error}</p> : null}
    {notice ? <p className="form-success" role="status">{notice}</p> : null}
    {confirmation ? <InstallConfirmationDialog summary={confirmation.summary} summaryHash={confirmation.summaryHash} busy={busy === "enable"} onConfirm={(value) => void enable(value)} onCancel={() => setConfirmation(null)} /> : null}
  </section>;
}
