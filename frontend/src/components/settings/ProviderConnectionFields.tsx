import { PROVIDER_LABELS } from "./providerOptions";
import { Button, FormField, Input } from "../ui";

type Props = {
  provider: string;
  apiKeys: Record<string, string>;
  onApiKeysChange: (next: Record<string, string>) => void;
  /** Whether the edited route already has a stored key. */
  editingApiKeySet: boolean;
  profileCredentialRef: string;
  onProfileCredentialRefChange: (value: string) => void;
  model: string;
  onModelChange: (value: string) => void;
  modelOptions: Array<{ id: string; name: string }>;
  modelLoadState: "idle" | "loading" | "error";
  modelError: string;
  busy: boolean;
  onLoadModels: () => void;
  baseUrl: string;
  onBaseUrlChange: (value: string) => void;
};

/**
 * The credential/model half of the route form: API key, credential reference,
 * model picker, and the provider-specific notes. Render-only — every value
 * and setter belongs to the profile form.
 */
export function ProviderConnectionFields({
  provider, apiKeys, onApiKeysChange, editingApiKeySet,
  profileCredentialRef, onProfileCredentialRefChange, model, onModelChange,
  modelOptions, modelLoadState, modelError, busy, onLoadModels,
  baseUrl, onBaseUrlChange,
}: Props) {
  const selectedProviderLabel = PROVIDER_LABELS[provider] || provider;
  // Credentials belong to a profile, not a provider type. In particular, a
  // second OpenAI-compatible profile must not appear to inherit the first
  // profile's stored secret merely because both use the same protocol.
  const selectedKeyAvailable = editingApiKeySet;
  const selectedKeyStatus = editingApiKeySet ? "Key saved" : "No key saved";
  return <>
    {provider !== "mock" ? (
      <>
        {provider === "codex" || provider === "gemini_cli" ? <p className="settings-provider-note settings-provider-field--wide">{provider === "codex" ? "Uses the protected Codex account session. No API key or credential reference is stored in Lynewright." : "Uses the official Gemini CLI with its own signed-in account. No API key or credential reference is stored in Lynewright; use the CLI's interactive /auth command on the runner host to manage the account."}</p> : <>
        <FormField label={`${selectedProviderLabel} API key`} htmlFor="ai-api-key" className="settings-provider-field settings-provider-field--wide">
          <div className="settings-key-control"><Input
            id="ai-api-key"
            type="password"
            autoComplete="off"
            value={apiKeys[provider] ?? ""}
            placeholder={selectedKeyAvailable ? "Saved key available — enter only to replace" : "Paste API key"}
            onChange={(event) => {
              const value = event.target.value;
              onApiKeysChange({ ...apiKeys, [provider]: value });
            }}
          />
          <span className={selectedKeyAvailable ? "key-status is-set" : "key-status"}>
            {selectedKeyStatus}
          </span>
          </div>
        </FormField>

        <label className="settings-provider-field settings-provider-field--wide" htmlFor="ai-credential-ref"><span>Credential reference</span>
          <input id="ai-credential-ref" value={profileCredentialRef} placeholder="e.g. ai.openai.production" spellCheck={false} onChange={(event) => onProfileCredentialRefChange(event.target.value)} />
          <small>Use a secret created in the Credential Manager. This is the preferred production path.</small>
        </label>
        </>}

        <FormField label="Model" htmlFor="ai-model" className="settings-provider-field settings-provider-field--wide">
          <div className="settings-model-control"><Input
            id="ai-model"
            list="ai-model-options"
            value={model}
            required
            placeholder="Load models or enter the exact model ID"
            onChange={(event) => onModelChange(event.target.value)}
          />
          <Button variant="secondary" disabled={busy || modelLoadState === "loading"} onClick={onLoadModels}>{modelLoadState === "loading" ? "Loading…" : "Load models"}</Button>
          </div>
        </FormField>
        <datalist id="ai-model-options">
          {modelOptions.map((option) => (
            <option key={option.id} value={option.id}>{option.name}</option>
          ))}
        </datalist>
        {modelLoadState === "error" ? <p className="error-message settings-provider-field--wide">{modelError}</p> : null}
      </>
    ) : (
      <p className="settings-provider-note settings-provider-field--wide">Deterministic mode requires no external key and remains available as a fallback.</p>
    )}

    {provider === "vertex" ? (
      <p className="settings-provider-note settings-provider-field--wide">Uses your Vertex AI API key against Google's global endpoint. Load models or enter an exact model ID.</p>
    ) : null}
    {provider === "codex" ? (
      <p className="settings-provider-note settings-provider-field--wide">Structured decisions run with read-only sandbox and denied tool approvals by default. Keep the route disabled until the protected Codex runtime is authenticated.</p>
    ) : null}
    {provider === "gemini_cli" ? (
      <p className="settings-provider-note settings-provider-field--wide">Structured text calls run through the protected runner with a per-job policy that denies every tool. The default model is the Gemini CLI's own auto selection.</p>
    ) : null}
    {provider === "openai_compatible" ? (
      <label className="settings-provider-field settings-provider-field--wide" htmlFor="ai-base-url"><span>Base URL</span>
          <input
            id="ai-base-url"
            type="url"
            value={baseUrl}
            required
            onChange={(event) => onBaseUrlChange(event.target.value)}
          />
          <small>Example: https://integrate.api.nvidia.com/v1</small>
      </label>
    ) : null}
  </>;
}
