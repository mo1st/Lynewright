import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../api/client";
import type {
  AiModelOption,
  AiProviderProfile,
  AiProviderProfilePayload,
  AiProviderTestResponse,
  AiSettingsPayload,
  AiSettingsRead,
} from "../../types";
import { AI_PROVIDERS, PROVIDER_LABELS } from "./providerOptions";
import { ProviderConnectionFields } from "./ProviderConnectionFields";
import { Button } from "../ui";

type Props = {
  busy: boolean;
  profileBusy: boolean;
  onProfileBusyChange: (busy: boolean) => void;
  /** The route being edited, or null for a new one. Field values follow it. */
  editing: AiProviderProfile | null;
  savedSettings: AiSettingsRead | null;
  onTest: (payload: AiSettingsPayload) => Promise<AiProviderTestResponse>;
  onProfileTestResult: (result: AiProviderTestResponse | null) => void;
  /** After a successful save the panel reloads the chain and clears editing. */
  onSaved: () => void;
  onCancelEdit: () => void;
};

/**
 * The new-route / edit-route form. It owns every field value; the owning
 * panel owns the chain list, the busy flag it shares with the list actions,
 * and the test result the diagnostics drawer displays.
 */
export function ProviderProfileForm({
  busy, profileBusy, onProfileBusyChange, editing, savedSettings,
  onTest, onProfileTestResult, onSaved, onCancelEdit,
}: Props) {
  const [provider, setProvider] = useState<string>("mock");
  const [profileName, setProfileName] = useState("");
  const [priority, setPriority] = useState(100);
  const [profileEnabled, setProfileEnabled] = useState(true);
  const [profileCredentialRef, setProfileCredentialRef] = useState("");
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [model, setModel] = useState("mock-brief-v1");
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [modelOptions, setModelOptions] = useState<AiModelOption[]>([]);
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [modelLoadState, setModelLoadState] = useState<"idle" | "loading" | "error">("idle");
  const [modelError, setModelError] = useState("");
  const [profileMessage, setProfileMessage] = useState("");
  const [profileError, setProfileError] = useState("");

  // Field values follow the edited route, else the saved settings. This
  // replaces the imperative fill the editor used to do.
  useEffect(() => {
    if (editing) {
      setProfileName(editing.name); setProvider(editing.provider);
      setModel(editing.model); setBaseUrl(editing.base_url); setPriority(editing.priority);
      setReasoningEffort(editing.reasoning_effort || "");
      setProfileEnabled(editing.enabled); setProfileCredentialRef(editing.credential_ref || "");
      setApiKeys({}); setModelOptions([]); onProfileTestResult(null);
      return;
    }
    if (!savedSettings) return;
    if ((savedSettings.providers ?? []).length) {
      // The editor creates a separate route; pre-filling it from the active
      // profile made the page look as though it was editing that route and
      // encouraged accidental duplicates.
      setProvider("openai_compatible");
      setModel("");
      setBaseUrl("");
    } else {
      // Preserve the legacy single-provider form until it is saved into the
      // profile chain for the first time.
      setProvider(savedSettings.provider);
      setModel(savedSettings.model);
      setBaseUrl(savedSettings.base_url);
    }
    setProfileName(""); setPriority(100); setProfileEnabled(true);
    setProfileCredentialRef(""); setApiKeys({}); setModelOptions([]);
  }, [editing, savedSettings]);

  const resetProfileForm = () => {
    onCancelEdit();
    setProfileName(""); setProvider("openai_compatible");
    setModel(""); setBaseUrl(""); setPriority(100); setProfileEnabled(true); setReasoningEffort("");
    setProfileCredentialRef("");
    setApiKeys({}); setModelOptions([]); onProfileTestResult(null);
  };

  const payload: AiSettingsPayload = {
    provider,
    api_keys: apiKeys,
    model: provider === "codex" ? model || "gpt-5.6-luna" : provider === "gemini_cli" ? model || "auto" : model,
    base_url: baseUrl
  };
  const protocolFor = (value: string): AiProviderProfilePayload["protocol"] =>
    value === "codex" ? "codex"
      : value === "gemini_cli" ? "gemini_cli"
      : value === "anthropic" ? "anthropic_messages"
      : value === "gemini" ? "gemini_generate"
        : value === "vertex" ? "vertex_generate" : "openai_chat";
  const profilePayload: AiProviderProfilePayload = {
    name: profileName.trim() || `${PROVIDER_LABELS[provider] || provider} ${model}`.trim(),
    provider,
    protocol: protocolFor(provider),
    model: provider === "codex" ? model || "gpt-5.6-luna" : provider === "gemini_cli" ? model || "auto" : model,
    reasoning_effort: provider === "codex" ? reasoningEffort : "",
    base_url: provider === "openai_compatible" || provider === "vertex" ? baseUrl : "",
    api_key: provider === "codex" || provider === "gemini_cli" ? null : apiKeys[provider]?.trim() || null,
    credential_ref: provider === "codex" || provider === "gemini_cli" ? "" : profileCredentialRef.trim(),
    priority,
    enabled: profileEnabled,
  };
  const editingProfileId = editing?.id ?? null;
  const providerReady = provider === "mock" || (
    Boolean(model.trim()) && (
      provider === "codex" || provider === "gemini_cli" || Boolean(apiKeys[provider]?.trim()) || Boolean(profileCredentialRef.trim()) || Boolean(editing?.api_key_set)
    )
  );

  const handleSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setProfileMessage("");
    setProfileError("");
    onProfileBusyChange(true);
    try {
      if (editingProfileId) await api.updateAiProviderProfile(editingProfileId, profilePayload);
      else await api.createAiProviderProfile(profilePayload);
      setProfileMessage(editingProfileId ? "Provider updated." : "Provider added to the fallback chain.");
      onSaved();
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : "Could not save this provider.");
    } finally {
      onProfileBusyChange(false);
    }
  };

  const loadModels = async () => {
    setModelLoadState("loading");
    setModelError("");
    try {
      const result = editingProfileId && !apiKeys[provider]?.trim()
        ? await api.getAiProviderProfileModels(editingProfileId)
        : await api.getAiModels(payload);
      if (!result.ok) {
        throw new Error(result.error || "Could not load provider models.");
      }
      setModelOptions(result.models);
      setModelLoadState("idle");
      if (!model && result.models.length > 0) {
        setModel(result.models[0].id);
      }
    } catch (error) {
      setModelOptions([]);
      setModelLoadState("error");
      setModelError(error instanceof Error ? error.message : "Could not load provider models.");
    }
  };

  const testCurrentValues = async () => {
    setProfileMessage("");
    setProfileError("");
    onProfileTestResult(null);
    onProfileBusyChange(true);
    try {
      const result = editingProfileId && !apiKeys[provider]?.trim()
        ? await api.testAiProviderProfile(editingProfileId)
        : await onTest(payload);
      onProfileTestResult(result);
      if (result.ok) {
        setProfileMessage("Connection successful.");
      } else {
        setProfileError(result.error || "Connection test failed.");
      }
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : "Could not test this provider.");
    } finally {
      onProfileBusyChange(false);
    }
  };

  return <form className="settings-form settings-provider-form" onSubmit={handleSave}>
    <div className="settings-provider-form__head"><p>Credentials and endpoint settings stay isolated to this route.</p></div>
    <div className="settings-provider-field-grid">
      <label className="settings-provider-field" htmlFor="ai-profile-name"><span>Configuration name</span><input id="ai-profile-name" value={profileName} required placeholder="e.g. NVIDIA NIM" onChange={(event) => setProfileName(event.target.value)} /></label>

      <label className="settings-provider-field settings-provider-field--priority" htmlFor="ai-priority"><span>Priority</span><input id="ai-priority" type="number" min="1" max="9999" value={priority} onChange={(event) => setPriority(Math.max(1, Number(event.target.value) || 1))} /><small>Lower runs first</small></label>
      <label className="settings-provider-field" htmlFor="ai-provider"><span>Provider</span><select
        id="ai-provider"
        value={provider}
        onChange={(event) => {
          const nextProvider = event.target.value;
          setProvider(nextProvider);
          setModel(
            nextProvider === "mock"
              ? "mock-brief-v1"
              : nextProvider === "codex"
                ? "gpt-5.6-luna"
                : nextProvider === "gemini_cli"
                  ? "auto"
                : savedSettings?.provider === nextProvider
                ? savedSettings.model
                : ""
          );
          setModelOptions([]);
          setModelError("");
        }}
      >
        {AI_PROVIDERS.map((p) => (
          <option key={p} value={p}>
            {PROVIDER_LABELS[p] || p}
          </option>
        ))}
      </select></label>
      <label className="settings-provider-toggle" htmlFor="ai-profile-enabled"><input id="ai-profile-enabled" type="checkbox" checked={profileEnabled} onChange={(event) => setProfileEnabled(event.target.checked)} /><span><strong>Enabled in fallback chain</strong><small>Paused routes stay saved but are skipped.</small></span></label>

      <ProviderConnectionFields
        provider={provider}
        apiKeys={apiKeys}
        onApiKeysChange={setApiKeys}
        editingApiKeySet={Boolean(editing?.api_key_set)}
        profileCredentialRef={profileCredentialRef}
        onProfileCredentialRefChange={setProfileCredentialRef}
        model={model}
        onModelChange={setModel}
        modelOptions={modelOptions}
        modelLoadState={modelLoadState}
        modelError={modelError}
        busy={busy}
        onLoadModels={() => void loadModels()}
        baseUrl={baseUrl}
        onBaseUrlChange={setBaseUrl}
      />
      {provider === "codex" ? (() => {
        const listed = modelOptions.find((option) => option.id === model);
        const efforts = listed?.efforts?.length ? listed.efforts : ["low", "medium", "high", "xhigh"];
        return <label className="settings-provider-field" htmlFor="ai-reasoning-effort"><span>Reasoning effort</span>
          <select id="ai-reasoning-effort" value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value)}>
            <option value="">Default{listed?.default_effort ? ` (${listed.default_effort})` : " (high)"}</option>
            {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            {reasoningEffort && !efforts.includes(reasoningEffort) ? <option value={reasoningEffort}>{reasoningEffort}</option> : null}
          </select>
          <small>Load models to see every model your ChatGPT account offers. Lower effort is faster and uses less Codex quota.</small>
        </label>;
      })() : null}
    </div>

    <div className="settings-provider-form__footer">
      <div className="settings-provider-feedback">
        {!providerReady ? <span className="is-warning" role="status">Add a key and model before saving.</span> : null}
        {profileMessage ? <span className="is-success" role="status" aria-live="polite">{profileMessage}</span> : null}
        {profileError ? <span className="is-error" role="alert" aria-live="assertive">{profileError}</span> : null}
      </div>
      <div className="action-group">
      <Button variant="primary" type="submit" busy={profileBusy} disabled={busy || !providerReady}>{editingProfileId ? "Save provider" : "Add provider"}</Button>
      <Button variant="secondary" type="button" disabled={busy || profileBusy || !providerReady} onClick={() => void testCurrentValues()}>{profileBusy ? "Testing…" : "Test current values"}</Button>
      {editingProfileId ? <Button variant="ghost" type="button" disabled={profileBusy} onClick={resetProfileForm}>Cancel</Button> : null}
      </div>
    </div>
  </form>;
}
