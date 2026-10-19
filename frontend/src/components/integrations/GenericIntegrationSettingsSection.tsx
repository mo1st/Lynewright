import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { CredentialMetadata } from "../../api/client";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationSettingSchemaField, IntegrationSettingsModel } from "../../types";
import CredentialReferencePicker from "./CredentialReferencePicker";
import DeclarativeField, { fieldValue } from "./DeclarativeField";

type Props = {
  integrationId: string;
  settings: IntegrationSettingsModel;
  credentialOptions: CredentialMetadata[];
  integrationEnabled: boolean;
  busy: boolean;
  onSave: (values: Record<string, unknown>) => void;
};

function credentialValue(value: unknown) {
  return value && typeof value === "object" && "credential_ref" in value ? String((value as { credential_ref?: unknown }).credential_ref ?? "") : typeof value === "string" ? value : "";
}

export default function GenericIntegrationSettingsSection({ integrationId, settings, credentialOptions, integrationEnabled, busy, onSave }: Props) {
  const [values, setValues] = useState(settings.values);
  useEffect(() => setValues(settings.values), [settings]);
  useEffect(() => { let active = true; void integrationManagementApi.settings(integrationId).then((response) => { if (active) setValues(response.values); }).catch(() => undefined); return () => { active = false; }; }, [integrationId]);
  const update = (field: IntegrationSettingSchemaField, value: unknown) => setValues((current) => ({ ...current, [field.key]: value }));

  return <section className="integration-subsection" aria-label="Operator settings">
    <header className="panel-header"><div><h4>Declared settings</h4><p className="muted-text">These fields are declared by the integration. Save changes here while it is enabled; they apply to the next safe operation. Secrets are selected from stored credentials and are never shown.</p></div></header>
    <form className="form-grid" onSubmit={(event) => { event.preventDefault(); onSave(values); }}>{settings.fields.map((field) => {
      const id = `integration-setting-${field.key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
      const value = fieldValue(field, values);
      if (field.type === "credential_ref") {
        const selected = credentialValue(value);
        return <div key={field.key} className="form-grid__wide"><CredentialReferencePicker id={id} selected={selected ? [selected] : []} options={credentialOptions} onChange={(next) => update(field, next[0] ?? "")} label={field.label} /><small className="muted-text">{field.description ?? "Stored references only; secret values are never returned."}</small></div>;
      }
      return <DeclarativeField key={field.key} field={field} id={id} value={value} disabled={busy} onChange={(next) => update(field, next)} />;
    })}<p className="muted-text form-grid__wide">Need a new API key? Add it in the <a href="#credential-management">Settings</a> section above, then select it here.</p><Button variant="primary" type="submit" disabled={busy}>Save and apply settings</Button></form>
  </section>;
}
