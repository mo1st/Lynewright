import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { confirmAction } from "../../context/ConfirmContext";
import type { AiProviderProfile, AiProviderProfilePayload, AiProviderTestResponse, AiSettingsPayload, AiSettingsRead } from "../../types";
import { Button, Dialog } from "../ui";
import { ProviderChainList } from "./ProviderChainList";
import { ProviderProfileForm } from "./ProviderProfileForm";
import { ProviderDiagnostics } from "./ProviderDiagnostics";

type Props = {
  busy: boolean;
  onTest: (payload: AiSettingsPayload) => Promise<AiProviderTestResponse>;
  savedSettings: AiSettingsRead | null;
  onSettingsChange?: (settings: AiSettingsRead) => void;
  testResult: AiProviderTestResponse | null;
  openDrawers: Record<string, boolean>;
  onToggleDrawer: (id: string, open: boolean) => void;
};

export function AiProvidersPanel({ busy, onTest, savedSettings, onSettingsChange, testResult, openDrawers, onToggleDrawer }: Props) {
  const [providerProfiles, setProviderProfiles] = useState<AiProviderProfile[]>([]);
  const [editingProfile, setEditingProfile] = useState<AiProviderProfile | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileTestResult, setProfileTestResult] = useState<AiProviderTestResponse | null>(null);
  const [testStatus, setTestStatus] = useState<Record<number, string>>({});
  const [profileError, setProfileError] = useState("");

  useEffect(() => { if (savedSettings) setProviderProfiles(savedSettings.providers ?? []); }, [savedSettings]);

  const reloadProfiles = async () => {
    const refreshed = await api.getAiSettings();
    setProviderProfiles(refreshed.providers ?? []);
    onSettingsChange?.(refreshed);
  };

  const openAdd = () => { setEditingProfile(null); setProfileError(""); setEditorOpen(true); };
  const openEdit = (profile: AiProviderProfile) => { setEditingProfile(profile); setProfileError(""); setEditorOpen(true); };
  const closeEditor = () => { setEditorOpen(false); setEditingProfile(null); };

  const removeProfile = async (id: number) => {
    if (!(await confirmAction({ title: "Remove this AI provider?", message: "It leaves the fallback chain, and requests route to the next provider in priority order.", confirmLabel: "Remove provider", tone: "danger" }))) return;
    setProfileError(""); setProfileBusy(true);
    try { await api.deleteAiProviderProfile(id); await reloadProfiles(); }
    catch (error) { setProfileError(error instanceof Error ? error.message : "Could not remove this provider."); }
    finally { setProfileBusy(false); }
  };

  const testProfile = async (profile: AiProviderProfile) => {
    setProfileBusy(true); setProfileError("");
    try {
      const result = await api.testAiProviderProfile(profile.id);
      setTestStatus((current) => ({ ...current, [profile.id]: result.ok ? "Passed" : "Failed" }));
      setProfileTestResult(result);
    } catch (error) {
      setTestStatus((current) => ({ ...current, [profile.id]: "Failed" }));
      setProfileError(error instanceof Error ? error.message : "Could not test this provider.");
    } finally { setProfileBusy(false); }
  };

  const toggleProfile = async (profile: AiProviderProfile) => {
    setProfileBusy(true); setProfileError("");
    try {
      const payload: AiProviderProfilePayload = {
        name: profile.name, provider: profile.provider, protocol: profile.protocol, model: profile.model,
        base_url: profile.base_url, credential_ref: profile.credential_ref, priority: profile.priority,
        enabled: !profile.enabled, reasoning_effort: profile.reasoning_effort, api_key: null,
      };
      await api.updateAiProviderProfile(profile.id, payload);
      await reloadProfiles();
    } catch (error) { setProfileError(error instanceof Error ? error.message : "Could not update this provider."); }
    finally { setProfileBusy(false); }
  };

  const statusText = providerProfiles.length
    ? `${providerProfiles.filter((item) => item.enabled).length} enabled`
    : savedSettings?.provider === "mock" ? "Deterministic fallback active" : savedSettings?.api_key_set ? "Legacy provider active" : "No routes configured";

  return <div className="settings-provider-workspace">
    <section className="panel settings-panel settings-provider-section" aria-labelledby="provider-routes-title">
      <header className="panel-header">
        <div><h2 id="provider-routes-title">AI providers</h2><p>Providers run in priority order. The deterministic fallback is always last.</p></div>
        <Button variant="primary" onClick={openAdd}>Add provider</Button>
      </header>
      <p className="settings-provider-status" role="status">Current routing: {statusText}</p>
      {profileError ? <p className="error-message" role="alert" aria-live="assertive">{profileError}</p> : null}
      <ProviderChainList profiles={providerProfiles} testStatus={testStatus} busy={profileBusy} onEdit={openEdit} onTest={(profile) => void testProfile(profile)} onToggle={(profile) => void toggleProfile(profile)} />
    </section>

    <ProviderDiagnostics savedSettings={savedSettings} testResult={testResult} profileTestResult={profileTestResult} openDrawers={openDrawers} onToggleDrawer={onToggleDrawer} />

    <Dialog open={editorOpen} title={editingProfile ? "Edit AI provider" : "Add AI provider"} description="Use a credential reference for saved secrets. Secret values are never shown after saving." onClose={closeEditor} className="settings-provider-dialog">
      <ProviderProfileForm
        busy={busy}
        profileBusy={profileBusy}
        onProfileBusyChange={setProfileBusy}
        editing={editingProfile}
        savedSettings={savedSettings}
        onTest={onTest}
        onProfileTestResult={setProfileTestResult}
        onSaved={() => void (async () => { await reloadProfiles(); closeEditor(); })()}
        onCancelEdit={closeEditor}
      />
    </Dialog>
  </div>;
}
