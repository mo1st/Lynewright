import { Button } from "../components/ui";
import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  setSessionToken,
  type MCPOAuthRequestInfo,
} from "../api/client";

type Props = { requestId: string | null };

function isReadCapability(scope: string): boolean {
  return scope.endsWith(":read")
    || ["integrations:discover", "system:diagnostics", "credentials:list", "credentials:status", "ai:read-config"].includes(scope);
}

function permissionLabel(scope: string): string {
  const [area, action] = scope.split(":");
  const name = area.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  const actionName = action ? action.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : "Access";
  return `${name} · ${actionName}`;
}

function describeRequestError(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "Only an active platform administrator can approve this platform-wide connection.";
  }
  if (error instanceof ApiError && error.status === 410) {
    return "This request expired. Return to your MCP client and start the connection again.";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "This request was already decided. Return to your MCP client to continue.";
  }
  return error instanceof Error ? error.message : "Could not load this authorization request.";
}

export default function McpOAuthConsent({ requestId }: Props) {
  const [request, setRequest] = useState<MCPOAuthRequestInfo | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmWrites, setConfirmWrites] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const writesSelected = selected.some((scope) => !isReadCapability(scope));

  useEffect(() => {
    document.title = "MCP access · Lynewright";
  }, []);

  useEffect(() => {
    if (!requestId) {
      setError("This authorization link is incomplete. Return to your MCP client and try again.");
      return;
    }
    let cancelled = false;
    api.getMcpOAuthRequest(requestId)
      .then((result) => {
        if (cancelled) return;
        setRequest(result);
        setSelected(result.default_scopes);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(describeRequestError(reason));
      });
    return () => { cancelled = true; };
  }, [requestId]);

  function toggle(scope: string) {
    setConfirmWrites(false);
    setSelected((current) => current.includes(scope)
      ? current.filter((value) => value !== scope)
      : [...current, scope]);
  }

  async function finish(approved: boolean) {
    if (!requestId || busy || (approved && (!selected.length || (writesSelected && !confirmWrites)))) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.decideMcpOAuthRequest(requestId, {
        approved,
        scopes: approved ? selected : [],
        confirm_writes: approved && confirmWrites,
      });
      // Authorization codes are returned only to this previously validated
      // callback. The Lynewright dashboard session remains in the Authorization
      // header and is never placed into the redirect URL.
      window.location.assign(result.redirect_to);
    } catch (reason) {
      setError(describeRequestError(reason));
      setBusy(false);
    }
  }

  async function switchAccount() {
    try {
      await api.logout();
    } catch {
      // Clearing the local session also works when the account is already expired.
    }
    setSessionToken("");
    window.location.reload();
  }

  const canApprove = Boolean(request && selected.length && (!writesSelected || confirmWrites) && !busy);

  return (
    <main className="login-screen mcp-oauth-screen">
      <section className="login-card mcp-oauth-card" aria-labelledby="mcp-oauth-title">
        <div className="login-brand-lockup">
          <span><strong>Lynewright</strong><small>Secure MCP connection</small></span>
        </div>
        <div>
          <h1 id="mcp-oauth-title">Connect an MCP application</h1>
          <p className="login-subtitle">Review the application and permissions before allowing access.</p>
        </div>

        {error ? <div className="mcp-oauth-error" role="alert">
          <p>{error}</p>
          {error.includes("platform administrator") ? (
            <Button variant="secondary" type="button" disabled={busy} onClick={() => void switchAccount()}>
              Switch account
            </Button>
          ) : null}
        </div> : null}

        {!request && !error ? <p role="status">Loading authorization request…</p> : null}
        {request ? (
          <>
            <dl className="mcp-oauth-summary">
              <div><dt>Application</dt><dd>{request.application_name}</dd></div>
              <div><dt>Callback origin</dt><dd><code>{request.redirect_origin}</code></dd></div>
              <div><dt>Signed in as</dt><dd>{request.account_email}</dd></div>
            </dl>
            <div className="mcp-oauth-platform-notice">
              <strong>Access applies across the Lynewright platform</strong>
              <p>This application name is supplied by its developer and is not a verified identity. Approving permissions can expose or change data available through platform MCP tools.</p>
            </div>

            <fieldset className="mcp-oauth-permissions" disabled={busy}>
              <legend>Permissions requested</legend>
              <p>Read permissions are selected by default. Choose only what this application needs.</p>
              {request.requested_scopes.map((scope) => (
                <label className="checkbox-row" key={scope}>
                  <input
                    type="checkbox"
                    checked={selectedSet.has(scope)}
                    onChange={() => toggle(scope)}
                  />
                  <span>{permissionLabel(scope)}</span>
                  <code>{scope}</code>
                </label>
              ))}
            </fieldset>

            {writesSelected ? (
              <label className="mcp-oauth-write-confirm">
                <input
                  type="checkbox"
                  checked={confirmWrites}
                  disabled={busy}
                  onChange={(event) => setConfirmWrites(event.target.checked)}
                />
                <span>I understand these selected permissions can change data or settings across the platform.</span>
              </label>
            ) : null}

            <p className="mcp-oauth-account-note">You are approving access as <strong>{request.account_email}</strong>, a platform administrator.</p>
            <div className="mcp-oauth-actions">
              <Button variant="secondary" type="button" disabled={busy} onClick={() => void finish(false)}>
                {busy ? "Please wait…" : "Cancel"}
              </Button>
              <Button variant="secondary" type="button" disabled={!canApprove} onClick={() => void finish(true)}>
                {busy ? "Continuing…" : "Allow access"}
              </Button>
            </div>
          </>
        ) : null}
      </section>
    </main>
  );
}
