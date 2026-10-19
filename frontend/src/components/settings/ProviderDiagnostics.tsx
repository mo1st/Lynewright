import { SETTINGS_ADVANCED_DRAWERS } from "../../navigation";
import type { AiProviderTestResponse, AiSettingsRead } from "../../types";

type Props = {
  savedSettings: AiSettingsRead | null;
  testResult: AiProviderTestResponse | null;
  profileTestResult: AiProviderTestResponse | null;
  openDrawers: Record<string, boolean>;
  onToggleDrawer: (id: string, open: boolean) => void;
};

/**
 * The two drawers under the fallback chain: live diagnostics and the advanced
 * provider plumbing. Render-only — test results flow in as props from the
 * panel that owns them, and the drawer open state lives with the page.
 */
export function ProviderDiagnostics({ savedSettings, testResult, profileTestResult, openDrawers, onToggleDrawer }: Props) {
  return <>
    <details className="settings-diagnostics settings-advanced" id={SETTINGS_ADVANCED_DRAWERS[0].id} open={Boolean(openDrawers[SETTINGS_ADVANCED_DRAWERS[0].id])} onToggle={(event) => onToggleDrawer(SETTINGS_ADVANCED_DRAWERS[0].id, event.currentTarget.open)}>
      <summary><strong>Provider status & diagnostics</strong><span>Active configuration and connection test result</span></summary>
    <div className="two-column-layout">
      <section className="panel">
        <h2>Active configuration</h2>
        {savedSettings ? (
          <dl className="definition-list">
            <div>
              <dt>Provider</dt>
              <dd>{savedSettings.provider}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd>{savedSettings.model}</dd>
            </div>
            <div>
              <dt>API key set</dt>
              <dd>{savedSettings.api_key_set ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Storage</dt>
              <dd>{savedSettings.storage_warning}</dd>
            </div>
          </dl>
        ) : (
          <p className="muted-text">No settings response yet.</p>
        )}
      </section>

      <section className="panel">
        <h2>Provider test</h2>
        {(profileTestResult ?? testResult) ? (
          <dl className="definition-list">
            <div>
              <dt>Status</dt>
              <dd>{(profileTestResult ?? testResult)!.ok ? "OK" : "Error"}</dd>
            </div>
            <div>
              <dt>Fallback available</dt>
              <dd>{(profileTestResult ?? testResult)!.fallback_available ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Error</dt>
              <dd>{(profileTestResult ?? testResult)!.error || "-"}</dd>
            </div>
            {/* "Error" alone cannot tell a rejected key from an overloaded
                provider, and the two need opposite responses. */}
            {(profileTestResult ?? testResult)!.hint ? (
              <div><dt>What to do</dt><dd>{(profileTestResult ?? testResult)!.hint}</dd></div>
            ) : null}
            {(profileTestResult ?? testResult)!.details?.failure_kind ? (
              <div><dt>Failure kind</dt><dd>{String((profileTestResult ?? testResult)!.details!.failure_kind)}</dd></div>
            ) : null}
            {(profileTestResult ?? testResult)!.details?.http_status ? (
              <div><dt>HTTP status</dt><dd>{String((profileTestResult ?? testResult)!.details!.http_status)}</dd></div>
            ) : null}
            {(profileTestResult ?? testResult)!.details?.endpoint ? (
              <div><dt>Endpoint</dt><dd>{String((profileTestResult ?? testResult)!.details!.endpoint)}</dd></div>
            ) : null}
            {(profileTestResult ?? testResult)!.details?.provider_response ? (
              <div><dt>Provider response</dt><dd><code>{String((profileTestResult ?? testResult)!.details!.provider_response)}</code></dd></div>
            ) : null}
          </dl>
        ) : (
          <p className="muted-text">Run a provider test to see the current backend result.</p>
        )}
      </section>
    </div>
    </details>
  </>;
}
