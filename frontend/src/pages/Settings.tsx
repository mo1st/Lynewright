import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AiProviderTestResponse, AiSettingsPayload, AiSettingsRead } from "../types";
import { OperatorSection } from "../components/operator/OperatorSection";
import { TelegramOperatorPanel } from "../components/operator/TelegramOperatorPanel";
import { AiProvidersPanel } from "../components/settings/AiProvidersPanel";
import { UsersPanel } from "../components/settings/UsersPanel";
import { CredentialManagementPanel } from "../components/settings/CredentialManagementPanel";
import { MediaManagementPanel } from "../components/settings/MediaManagementPanel";
import type { SettingsPanel } from "../navigation";

type Props = { panel?: SettingsPanel; currentUserId?: number };
export default function Settings({ panel = "providers", currentUserId = 0 }: Props) {
  const [savedSettings, setSavedSettings] = useState<AiSettingsRead | null>(null);
  const [testResult, setTestResult] = useState<AiProviderTestResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [openDrawers, setOpenDrawers] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (panel !== "providers") return;
    let active = true;
    api.getAiSettings().then((value) => { if (active) setSavedSettings(value); }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "AI settings could not be loaded."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [panel]);
  const test = async (payload: AiSettingsPayload): Promise<AiProviderTestResponse> => {
    setBusy(true); setError("");
    try { const result = await api.testAiSettings(payload); setTestResult(result); return result; }
    finally { setBusy(false); }
  };
  return <div className="page-stack settings-page">
    {error ? <p role="alert" className="form-error">{error}</p> : null}
    {panel === "providers" ? loading ? <p role="status">Loading provider settings…</p> : <AiProvidersPanel busy={busy} onTest={test} savedSettings={savedSettings} onSettingsChange={setSavedSettings} testResult={testResult} openDrawers={openDrawers} onToggleDrawer={(id, open) => setOpenDrawers((current) => ({ ...current, [id]: open }))} /> : null}
    {panel === "operator" ? <OperatorSection view="operator" /> : null}
    {panel === "telegram" ? <TelegramOperatorPanel /> : null}
    {panel === "users" ? <UsersPanel currentUserId={currentUserId} /> : null}
    {panel === "credentials" ? <CredentialManagementPanel /> : null}
    {panel === "media" ? <MediaManagementPanel /> : null}
  </div>;
}
