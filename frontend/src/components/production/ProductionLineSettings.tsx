import GenericIntegrationSettingsSection from "../integrations/GenericIntegrationSettingsSection";
import type { CredentialMetadata } from "../../api/client";
import type { IntegrationSettingsModel } from "../../types";

/** A compatibility component that renders only fields declared by an integration. */
export default function ProductionLineSettings({ integrationId, settings, credentialOptions = [], enabled, busy = false, onSave }: {
  integrationId: string;
  settings: IntegrationSettingsModel;
  credentialOptions?: CredentialMetadata[];
  enabled: boolean;
  busy?: boolean;
  onSave(values: Record<string, unknown>): void;
}) {
  return <section className="production-view" aria-label="Declared line settings">
    <h2>Declared line settings</h2>
    <GenericIntegrationSettingsSection integrationId={integrationId} settings={settings} credentialOptions={credentialOptions} integrationEnabled={enabled} busy={busy} onSave={onSave} />
  </section>;
}
