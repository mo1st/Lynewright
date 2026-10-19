import { Button, StatusBadge, Table, type TableColumn } from "../ui";
import type { AiProviderProfile } from "../../types";
import { PROVIDER_LABELS } from "./providerOptions";

type Props = {
  profiles: AiProviderProfile[];
  testStatus: Record<number, string>;
  busy: boolean;
  onEdit: (profile: AiProviderProfile) => void;
  onTest: (profile: AiProviderProfile) => void;
  onToggle: (profile: AiProviderProfile) => void;
};

type ProviderRow = AiProviderProfile | null;

/** Configured routes are shown in their effective priority order. The final
 * fixed row documents the deterministic fallback used when no route succeeds. */
export function ProviderChainList({ profiles, testStatus, busy, onEdit, onTest, onToggle }: Props) {
  const rows: ProviderRow[] = [...profiles].sort((a, b) => a.priority - b.priority);
  rows.push(null);
  const columns: Array<TableColumn<ProviderRow>> = [
    { key: "name", header: "Name", render: (profile) => <span className="settings-provider-name">{profile ? <><strong>{profile.name}</strong><small className="muted-text">{PROVIDER_LABELS[profile.provider] || profile.provider}</small></> : <><strong>Deterministic fallback</strong><small className="muted-text">Fixed last route · no external service</small></>}</span> },
    { key: "provider", header: "Provider", render: (profile) => profile ? PROVIDER_LABELS[profile.provider] || profile.provider : "Built in" },
    { key: "model", header: "Model", render: (profile) => profile?.model ?? "Deterministic" },
    { key: "enabled", header: "Enabled", render: (profile) => profile ? <StatusBadge status={profile.enabled ? "enabled" : "disabled"}>{profile.enabled ? "Enabled" : "Disabled"}</StatusBadge> : <StatusBadge status="enabled">Always available</StatusBadge> },
    { key: "priority", header: "Priority", render: (profile) => profile ? profile.priority : "Last" },
    { key: "last-test", header: "Last test result", render: (profile) => profile ? testStatus[profile.id] ?? "Not tested this session" : "Always ready" },
  ];
  return <Table
    label="AI provider priority"
    columns={columns}
    rows={rows}
    getRowKey={(profile) => profile?.id ?? "deterministic-fallback"}
    empty="No saved providers. The deterministic fallback remains available."
    rowActions={(profile) => profile ? <div className="action-group">
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => onEdit(profile)}>Edit</Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => onTest(profile)}>Test</Button>
      <Button size="sm" variant={profile.enabled ? "danger" : "secondary"} disabled={busy} onClick={() => onToggle(profile)}>{profile.enabled ? "Disable" : "Enable"}</Button>
    </div> : <span className="muted-text">Always last</span>}
  />;
}
