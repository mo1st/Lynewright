import { useCallback, useEffect, useState } from "react";
import { API_BASE_URL, describeApiError } from "../../api/client";
import { useProject } from "../../context/ProjectContext";
import { interactionApi, operatorApi } from "../../api/operatorClient";
import type {
  InteractionConnection,
  InteractionLink,
  InteractionLinkToken,
  NotificationRoute,
  OperatorProfile,
} from "../../types";
import { Badge, Button, Section, Table } from "../ui";
import { formatWhen } from "./shared";
import { confirmAction } from "../../context/ConfirmContext";
import { navigate } from "../../router";
import { settingsPath } from "../../navigation";
import { FULL_OPERATOR_CAPABILITIES, PUBLIC_OPERATOR_CAPABILITIES } from "./operatorCapabilities";

const INTENTS = ["IMMEDIATE", "DIGEST", "SILENT"] as const;

const SUGGESTED_EVENT_CLASSES = ["critical", "approval_requested", "digest", "operator_result"];

const LINK_TTL_OPTIONS = [
  { label: "15 minutes", ttl: 900 },
  { label: "1 hour", ttl: 3600 },
  { label: "6 hours", ttl: 21600 },
] as const;

/** One-time secrets live here only until dismissed or replaced — they are
 *  never fetched again and never rendered anywhere else. */
type RevealedSecret = { title: string; webhookUrl: string; secret: string } | null;

export function TelegramOperatorPanel() {
  const { projectId, loading: projectLoading } = useProject();
  const [connections, setConnections] = useState<InteractionConnection[] | null>(null);
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [links, setLinks] = useState<InteractionLink[] | null>(null);
  const [routes, setRoutes] = useState<NotificationRoute[] | null>(null);
  const [operatorProfiles, setOperatorProfiles] = useState<OperatorProfile[]>([]);
  const [selectedOperatorProfileId, setSelectedOperatorProfileId] = useState("");
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busyId, setBusyId] = useState("");

  // connection create
  const [newName, setNewName] = useState("");
  const [newToken, setNewToken] = useState("");
  const [creating, setCreating] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState<RevealedSecret>(null);

  // linking
  const [linkToken, setLinkToken] = useState<InteractionLinkToken | null>(null);
  const [linkTtl, setLinkTtl] = useState<number>(3600);

  // routing
  const [routeClass, setRouteClass] = useState("critical");
  const [routeIntent, setRouteIntent] = useState<string>("IMMEDIATE");

  const selected = connections?.find((item) => item.id === selectedConnectionId) ?? null;

  const loadAll = useCallback(async () => {
    if (projectLoading || !projectId) {
      setConnections([]);
      setRoutes([]);
      setOperatorProfiles([]);
      setSelectedConnectionId("");
      setLoadError("");
      return;
    }
    try {
      const [nextConnections, nextRoutes, nextProfiles] = await Promise.all([
        interactionApi.listConnections(projectId),
        interactionApi.listNotificationRoutes(projectId),
        operatorApi.listProfiles(projectId).catch(() => []),
      ]);
      setConnections(nextConnections);
      setRoutes(nextRoutes);
      setOperatorProfiles(nextProfiles);
      setSelectedConnectionId((current) => (
        current && nextConnections.some((item) => item.id === current)
          ? current
          : nextConnections[0]?.id ?? ""
      ));
      setLoadError("");
    } catch (caught) {
      setConnections(null);
      setLoadError(describeApiError(caught, "Could not load Telegram settings."));
    }
  }, [projectId, projectLoading]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  useEffect(() => {
    let cancelled = false;
    if (projectLoading || !projectId || !selectedConnectionId) { setLinks(null); return; }
    interactionApi.listLinks(selectedConnectionId)
      .then((next) => { if (!cancelled) setLinks(next); })
      .catch(() => { if (!cancelled) setLinks(null); });
    return () => { cancelled = true; };
  }, [projectId, projectLoading, selectedConnectionId]);

  useEffect(() => {
    setSelectedOperatorProfileId(selected?.operator_profile_id ?? "");
  }, [selected?.id, selected?.operator_profile_id]);

  async function run(action: () => Promise<void>, busyKey: string) {
    setError(""); setMessage(""); setBusyId(busyKey);
    try { await action(); }
    catch (caught) { setError(describeApiError(caught, "The change could not be applied.")); }
    finally { setBusyId(""); }
  }

  async function createConnection() {
    if (!newName.trim()) { setError("Give the connection a name."); return; }
    if (!projectId) { setError("Select a project first — connections are project-scoped."); return; }
    setCreating(true); setError(""); setMessage("");
    try {
      const created = await interactionApi.createConnection({
        project_id: projectId,
        name: newName.trim(),
        bot_token: newToken.trim(),
      });
      setNewToken("");
      setNewName("");
      setComposerOpen(false);
      await loadAll();
      setSelectedConnectionId(created.id);
      const base = API_BASE_URL.replace(/\/$/, "");
      const path = created.webhook_path ?? `/api/interactions/telegram/${created.id}/webhook`;
      setRevealedSecret({
        title: created.bot_username ? `Webhook for @${created.bot_username}` : "Webhook setup",
        webhookUrl: `${base}${path}`,
        secret: created.webhook_secret || "(no secret required)",
      });
      setMessage("Connection created. Copy the webhook details now — the secret is not shown again.");
    } catch (caught) {
      setError(describeApiError(caught, "Could not create the Telegram connection."));
    } finally {
      setCreating(false);
    }
  }

  async function rotateSecret(connection: InteractionConnection) {
    if (!(await confirmAction({
      title: "Rotate the webhook secret?",
      message: "The old URL stops working immediately, so update anything pointing at it.",
      confirmLabel: "Rotate secret",
      tone: "danger",
    }))) return;
    await run(async () => {
      const result = await interactionApi.rotateWebhookSecret(connection.id);
      const base = API_BASE_URL.replace(/\/$/, "");
      setRevealedSecret({
        title: "Rotated webhook",
        webhookUrl: `${base}/api/interactions/telegram/${result.id}/webhook`,
        secret: result.webhook_secret || "(no secret required)",
      });
      setMessage("Secret rotated. Copy it now — it is not shown again.");
    }, connection.id);
  }

  async function generateLinkToken() {
    if (!selected || !projectId) return;
    await run(async () => {
      const token = await interactionApi.createLinkToken({
        connection_id: selected.id,
        project_id: projectId,
        ttl_seconds: linkTtl,
      });
      setLinkToken(token);
    }, "link-token");
  }

  async function revokeLink(link: InteractionLink) {
    if (!(await confirmAction({
      title: "Revoke this Telegram link?",
      message: "This account will stop receiving Telegram notifications and approval prompts.",
      confirmLabel: "Revoke link",
      tone: "danger",
    }))) return;
    await run(async () => {
      await interactionApi.revokeLink(link.id);
      setLinks((current) => (current ?? []).filter((item) => item.id !== link.id));
      setMessage("Telegram account unlinked.");
    }, link.id);
  }

  async function saveRoute(eventClass: string) {
    if (!projectId) return;
    const trimmed = eventClass.trim().toLowerCase();
    if (!trimmed) { setError("Name an event class to route."); return; }
    await run(async () => {
      const saved = await interactionApi.upsertNotificationRoute({
        project_id: projectId,
        event_class: trimmed,
        intent: routeIntent,
        channels: [{ channel: "telegram", chat_ref: selected?.external_identity ?? "" }],
        enabled: true,
      });
      setRoutes((current) => {
        const rest = (current ?? []).filter((item) => item.event_class !== saved.event_class);
        return [...rest, saved].sort((a, b) => a.event_class.localeCompare(b.event_class));
      });
      setMessage(`"${trimmed}" now routes to Telegram.`);
    }, `route-${eventClass}`);
  }

  async function deleteRoute(route: NotificationRoute) {
    if (!projectId) return;
    if (!(await confirmAction({
      title: `Remove the ${route.event_class} route?`,
      message: "Future notifications in this event class will no longer be delivered to Telegram.",
      confirmLabel: "Remove route",
      tone: "danger",
    }))) return;
    await run(async () => {
      await interactionApi.deleteNotificationRoute(route.event_class, projectId);
      setRoutes((current) => (current ?? []).filter((item) => item.id !== route.id));
    }, `route-${route.id}`);
  }

  async function saveTelegramAgent() {
    if (!selected) return;
    const chosen = operatorProfiles.find((profile) => profile.id === selectedOperatorProfileId);
    await run(async () => {
      await interactionApi.updateConnection(selected.id, { operator_profile_id: selectedOperatorProfileId });
      await loadAll();
      setMessage(chosen
        ? `Telegram messages will use the ${chosen.name} agent role.`
        : "Telegram will use the first enabled agent role for this project.");
    }, `agent-${selected.id}`);
  }

  async function grantAllAgentAccess() {
    if (!selected) return;
    const chosen = operatorProfiles.find((profile) => profile.id === selectedOperatorProfileId)
      ?? operatorProfiles.find((profile) => profile.enabled);
    if (!chosen) {
      setError("Create or enable an Operator role in Settings, then Operator first.");
      return;
    }
    if (!(await confirmAction({
      title: `Grant all permissions to ${chosen.name}?`,
      message: `This enables all ${PUBLIC_OPERATOR_CAPABILITIES.length} public Operator capabilities for Telegram. Sensitive actions still require your approval in Telegram.`,
      confirmLabel: "Grant all permissions",
    }))) return;
    await run(async () => {
      await operatorApi.updateProfile(chosen.id, {
        granted_capabilities: FULL_OPERATOR_CAPABILITIES,
      });
      setSelectedOperatorProfileId(chosen.id);
      await loadAll();
      setMessage(`${chosen.name} now has all ${PUBLIC_OPERATOR_CAPABILITIES.length} public permissions. Sensitive actions still require approval.`);
    }, `agent-access-${selected.id}`);
  }

  async function sendTest(routeClassToTest: string) {
    if (!projectId) return;
    await run(async () => {
      const result = await interactionApi.sendTestNotification({
        project_id: projectId,
        event_class: routeClassToTest,
        title: "Lynewright test notification",
        body: "If you can read this on Telegram, the route works.",
        intent: routeIntent,
      });
      setMessage(result.dispatched
        ? "Test notification sent. Check your Telegram."
        : `Not delivered${result.detail ? `: ${result.detail}` : ". Check the route's channel target."}`);
    }, "test-send");
  }

  async function copySecret(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(`${label} copied to the clipboard.`);
      setError("");
    } catch {
      setError(`Could not copy the ${label.toLowerCase()}. Select it and copy manually.`);
    }
  }

  const routedClasses = new Set((routes ?? []).filter((route) => route.enabled).map((route) => route.event_class));
  const verifiedConnection = (connections ?? []).find((connection) => connection.enabled && connection.external_identity);
  const configuredConnection = (connections ?? []).some((connection) => connection.enabled);

  return (
    <div className="telegram-settings-stack">
      <Section title="Telegram" description="Connect a bot, link your account, and choose which notifications reach you there." className="telegram-settings-section">

      {message ? <p className="status-message" role="status">{message}</p> : null}
      {error ? <p className="error-message" role="alert">{error}</p> : null}
      {loadError ? <div className="operator-load-error" role="alert"><p className="error-message">{loadError}</p><Button variant="secondary" type="button" disabled={busyId !== "" || creating} onClick={() => void loadAll()}>Retry Telegram settings</Button></div> : null}
      {connections === null && !loadError ? <p className="muted-text">Loading connections…</p> : null}
      <div className="connection-card" role="status" aria-label="Telegram connection status">
        <div className="section-heading"><div><strong>Connection status</strong><p>{verifiedConnection ? `Connected as ${verifiedConnection.external_identity}.` : configuredConnection ? "A connection is saved but still needs bot verification." : "No Telegram bot is configured."}</p></div>
          <Badge tone={verifiedConnection ? "positive" : configuredConnection ? "attention" : "neutral"}>{verifiedConnection ? "Connected" : configuredConnection ? "Needs verification" : "Not configured"}</Badge>
        </div>
        {!configuredConnection ? <p className="muted-text">Create a Telegram connection with a bot token, verify the bot, then link a chat to receive notifications.</p> : null}
      </div>

      {revealedSecret ? (
        <div className="secret-box" role="status" style={{ marginBottom: 14 }}>
          <strong>{revealedSecret.title}</strong>
           <code>{revealedSecret.webhookUrl}</code>
           <code>secret header value: {revealedSecret.secret}</code>
           <small>Set this URL as your bot's webhook. The secret is shown once.</small>
           <div className="inline-actions" aria-label="Webhook secret actions">
             <Button variant="secondary" type="button" onClick={() => void copySecret(revealedSecret.webhookUrl, "Webhook URL")}>Copy webhook URL</Button>
             <Button variant="secondary" type="button" onClick={() => void copySecret(revealedSecret.secret, "Webhook secret")}>Copy secret</Button>
             <Button variant="secondary" type="button" onClick={() => setRevealedSecret(null)}>Done</Button>
           </div>
        </div>
      ) : null}
      </Section>

      <Section title="Connections" description="Add a bot connection and verify it before linking chats." headingLevel={3} className="telegram-settings-section">
      {connections !== null && connections.length === 0 ? (
        <p className="muted-text" role="status">No Telegram connection yet. You will need a bot token from @BotFather.</p>
      ) : null}
      {connections !== null && connections.length > 0 ? (
        <ol className="settings-provider-chain" aria-label="Telegram connections">
          {connections.map((connection) => (
            <li className="settings-provider-chain-item operator-list-item operator-connection-item" key={connection.id}>
              <div className="settings-provider-copy">
                <div className="settings-provider-title">
                  <strong>{connection.name}</strong>
                  <Badge tone={connection.enabled ? "positive" : "neutral"}>{connection.enabled ? "Enabled" : "Disabled"}</Badge>
                  {connection.external_identity
                    ? <Badge tone={connection.verified_at ? "positive" : "attention"}>{connection.external_identity}{connection.verified_at ? "" : " (unverified)"}</Badge>
                    : <Badge tone="attention">Unverified</Badge>}
                </div>
                <div className="settings-provider-meta"><span>created {formatWhen(connection.created_at)}</span></div>
              </div>
              <div className="inline-actions" aria-label={`Actions for ${connection.name}`}>
                <Button variant="secondary" type="button" onClick={() => { setSelectedConnectionId(connection.id); setLinkToken(null); }}>
                  {selectedConnectionId === connection.id ? "Selected" : "Select"}
                </Button>
                <Button variant="secondary" type="button" disabled={busyId === `${connection.id}-verify`} onClick={() => void run(async () => { const updated = await interactionApi.verifyConnection(connection.id); await loadAll(); setMessage(updated.external_identity ? `Verified as ${updated.external_identity}.` : "Verified, but the bot returned no username."); }, `${connection.id}-verify`)}>Verify bot</Button>
                <Button variant="secondary" type="button" disabled={busyId === connection.id} onClick={() => void run(async () => { await interactionApi.updateConnection(connection.id, { enabled: !connection.enabled }); await loadAll(); }, connection.id)}>{connection.enabled ? "Disable" : "Enable"}</Button>
                <Button variant="secondary" type="button" disabled={busyId === `${connection.id}-rotate`} onClick={() => void rotateSecret(connection)}>Rotate secret</Button>
                <Button variant="danger" type="button"  disabled={busyId === connection.id} onClick={() => void run(async () => { if (!(await confirmAction({ title: `Delete the "${connection.name}" connection?`, message: "Messages stop being received on this number.", confirmLabel: "Delete connection", tone: "danger", }))) return; await interactionApi.deleteConnection(connection.id); if (selectedConnectionId === connection.id) setSelectedConnectionId(""); await loadAll(); }, connection.id)}>Delete</Button>
              </div>
            </li>
          ))}
        </ol>
      ) : null}

      {!composerOpen ? (
        <div className="action-group">
          <Button variant="secondary" type="button" onClick={() => setComposerOpen(true)}>Add a Telegram connection</Button>
        </div>
      ) : null}

      {composerOpen ? <form className="settings-form" onSubmit={(event) => { event.preventDefault(); void createConnection(); }}>
        <span className="eyebrow">New connection</span>
        <div className="settings-grid">
          <label className="alert-field" htmlFor="telegram-connection-name">
            <span>Name</span>
            <input id="telegram-connection-name" name="telegram_connection_name" autoComplete="off" value={newName} maxLength={120} required
              placeholder="e.g. Store alerts bot"
              onChange={(event) => setNewName(event.target.value)} />
          </label>
          <label className="alert-field" htmlFor="telegram-bot-token">
            <span>Bot token</span>
            <input id="telegram-bot-token" name="telegram_bot_token" type="password" autoComplete="off" value={newToken}
              placeholder="Paste from @BotFather"
              onChange={(event) => setNewToken(event.target.value)} />
            <small>Sent once to the server, never displayed or stored readable again.</small>
          </label>
        </div>
        <div className="action-group">
          <Button variant="primary" type="submit" disabled={creating}>{creating ? "Creating…" : "Create connection"}</Button>
          <Button variant="secondary" type="button" disabled={creating} onClick={() => setComposerOpen(false)}>Cancel</Button>
        </div>
      </form> : null}
      </Section>

      <Section title="Telegram message agent" description="Choose which operator role responds to messages from the selected bot." headingLevel={3} className="telegram-settings-section">
      {!selected ? (
        <p className="muted-text">Select a Telegram connection above to choose the agent role that answers its messages.</p>
      ) : (
        <>
          <div className="settings-grid">
            <label className="alert-field" htmlFor="telegram-agent-connection">
              <span>Telegram connection</span>
              <select id="telegram-agent-connection" name="telegram_agent_connection" value={selectedConnectionId}
                onChange={(event) => setSelectedConnectionId(event.target.value)}>
                {(connections ?? []).map((connection) => (
                  <option key={connection.id} value={connection.id}>{connection.name}</option>
                ))}
              </select>
            </label>
            <label className="alert-field" htmlFor="telegram-agent-profile">
              <span>Agent role</span>
              <select id="telegram-agent-profile" name="telegram_agent_profile" value={selectedOperatorProfileId}
                onChange={(event) => setSelectedOperatorProfileId(event.target.value)}>
                <option value="">Automatic — first enabled role (existing behavior)</option>
                {operatorProfiles.map((profile) => (
                  <option key={profile.id} value={profile.id} disabled={!profile.enabled}>
                    {profile.name}{profile.enabled ? "" : " (disabled)"} — {profile.max_steps} steps
                  </option>
                ))}
              </select>
              <small>Choose the enabled role that responds to this bot. Manage its permissions and limits in Settings, then Operator.</small>
            </label>
          </div>
          <div className="action-group">
            <Button variant="secondary" type="button" disabled={busyId === `agent-${selected.id}`} onClick={() => void saveTelegramAgent()}>
              {busyId === `agent-${selected.id}` ? "Saving…" : "Save message agent"}
            </Button>
            <Button variant="secondary" type="button" disabled={busyId === `agent-access-${selected.id}` || operatorProfiles.length === 0} onClick={() => void grantAllAgentAccess()}>
              {busyId === `agent-access-${selected.id}` ? "Granting…" : "Grant all permissions"}
            </Button>
            <Button variant="secondary" type="button" onClick={() => navigate(settingsPath("operator"))}>
              Edit role permissions
            </Button>
          </div>
          <p className="muted-text" style={{ marginTop: 8 }}>
            The shortcut updates the selected role with all {PUBLIC_OPERATOR_CAPABILITIES.length} public capabilities. Publication, credential, and other risky actions still pause for your approval.
          </p>
        </>
      )}
      </Section>

      <Section title="Account linking" description="Link a Telegram chat so it can receive notifications and approval prompts." headingLevel={3} className="telegram-settings-section">
      {!selected ? (
        <p className="muted-text" data-testid="telegram-not-linked">
          No connection selected yet — create one above before linking your Telegram account.
        </p>
      ) : (
        <>
          <div className="settings-grid">
            <label className="alert-field" htmlFor="telegram-link-connection">
              <span>Connection</span>
              <select id="telegram-link-connection" name="telegram_link_connection" value={selectedConnectionId}
                onChange={(event) => { setSelectedConnectionId(event.target.value); setLinkToken(null); }}>
                {(connections ?? []).map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {connection.name}{connection.external_identity ? ` (${connection.external_identity})` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="alert-field" htmlFor="telegram-link-ttl">
              <span>Code valid for</span>
              <select id="telegram-link-ttl" name="telegram_link_ttl" value={linkTtl}
                onChange={(event) => setLinkTtl(Number(event.target.value))}>
                {LINK_TTL_OPTIONS.map((option) => (
                  <option key={option.ttl} value={option.ttl}>{option.label}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="action-group">
            <Button variant="secondary" type="button" onClick={() => void generateLinkToken()} disabled={busyId === "link-token"}>
              Generate Telegram link code
            </Button>
          </div>
          {linkToken ? (
            <div className="secret-box" role="status" data-testid="telegram-link-code">
              <strong>One-time link code</strong>
              <code style={{ fontSize: "1rem" }}>{linkToken.token}</code>
              <small>
                In Telegram, open your bot and send <code>/start {linkToken.token}</code>. Expires{" "}
                {formatWhen(linkToken.expires_at_epoch)}. Shown once — generate a new code when it lapses.
              </small>
            </div>
          ) : null}

          <Section title="Linked accounts" description="Chats currently connected to this Telegram bot." headingLevel={4} className="telegram-settings-subsection">
          {links === null ? <p className="muted-text">Loading links…</p> : null}
          {links !== null && links.length === 0 ? (
            <p className="muted-text" role="status">
              Not linked yet. Generate a code above and send it to your bot to receive notifications and approvals here.
            </p>
          ) : null}
          {links !== null && links.length > 0 ? (
            <Table label="Linked Telegram accounts" rows={links} getRowKey={(link) => link.id} columns={[
              { key: "account", header: "Account", render: (link) => link.external_username ? `@${link.external_username}` : link.external_user_id },
              { key: "status", header: "Status", render: (link) => <Badge tone={link.status === "active" ? "positive" : "neutral"}>{link.status}</Badge> },
              { key: "linked", header: "Linked", render: (link) => formatWhen(link.created_at) },
            ]} rowActions={(link) => <Button variant="danger" size="sm" disabled={busyId === link.id} onClick={() => void revokeLink(link)}>Revoke</Button>} />
          ) : null}
          </Section>
        </>
      )}
      </Section>

      <Section title="Notification routing" description="Choose which project events are sent to Telegram." headingLevel={3} className="telegram-settings-section">
      {!projectId ? (
        <p className="muted-text">No project selected, so notification routes cannot be managed here right now.</p>
      ) : !selected ? (
        // A route's only channel here is the selected connection's chat, so
        // without one `saveRoute` would persist a route with an empty chat_ref
        // and "Send test notification" would have nowhere to send.
        <p className="muted-text" data-testid="telegram-routing-unavailable">
          Routing needs a Telegram connection — add one above first.
        </p>
      ) : (
        <>
          <div className="settings-grid">
            <label className="alert-field" htmlFor="telegram-route-class">
              <span>Event class</span>
              <input id="telegram-route-class" name="telegram_event_class" autoComplete="off" value={routeClass} maxLength={100}
                placeholder="critical"
                onChange={(event) => setRouteClass(event.target.value)} />
              <small>
                Suggested: {" "}
                {SUGGESTED_EVENT_CLASSES.map((suggestion) => (
                  <Button variant="secondary" key={suggestion} type="button" onClick={() => setRouteClass(suggestion)}>{suggestion}</Button>
                ))}
              </small>
            </label>
            <label className="alert-field" htmlFor="telegram-route-intent">
              <span>Delivery</span>
              <select id="telegram-route-intent" name="telegram_delivery_intent" value={routeIntent}
                onChange={(event) => setRouteIntent(event.target.value)}>
                {INTENTS.map((intent) => <option key={intent} value={intent}>{intent.toLowerCase()}</option>)}
              </select>
              <small>immediate sends at once, digest batches, silent logs only.</small>
            </label>
          </div>
          <div className="action-group">
            <Button variant="secondary" type="button" disabled={busyId === `route-${routeClass}`} onClick={() => void saveRoute(routeClass)}>
              {routedClasses.has(routeClass.trim().toLowerCase()) ? "Update route" : "Route to Telegram"}
            </Button>
            <Button variant="secondary" type="button" disabled={busyId === "test-send"} onClick={() => void sendTest(routeClass.trim().toLowerCase() || "critical")}>
              {busyId === "test-send" ? "Sending…" : "Send test notification"}
            </Button>
          </div>

          {routes !== null && routes.length > 0 ? (
            <Table label="Telegram notification routes" rows={routes} getRowKey={(route) => route.id} columns={[
              { key: "event", header: "Event", render: (route) => route.event_class },
              { key: "delivery", header: "Delivery", render: (route) => `${route.intent.toLowerCase()} · ${route.channels.map((channel) => channel.channel).join(", ")}` },
              { key: "status", header: "Status", render: (route) => <Badge tone={route.enabled ? "positive" : "neutral"}>{route.enabled ? "Routed" : "Paused"}</Badge> },
            ]} rowActions={(route) => <Button variant="danger" size="sm" disabled={busyId === `route-${route.id}`} onClick={() => void deleteRoute(route)}>Remove route</Button>} />
          ) : (
            <p className="muted-text" role="status">
              No routes configured for this project — nothing reaches Telegram until you add one above.
            </p>
          )}
        </>
      )}
      </Section>
    </div>
  );
}
