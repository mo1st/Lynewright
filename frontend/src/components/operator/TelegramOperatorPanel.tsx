import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { describeApiError } from "../../api/client";
import { useProject } from "../../context/ProjectContext";
import { interactionApi, operatorApi } from "../../api/operatorClient";
import type {
  InteractionConnection,
  InteractionLink,
  InteractionLinkToken,
  NotificationRoute,
  OperatorProfile,
  TelegramSettings,
} from "../../types";
import { Badge, Banner, Button, FormField, Input, Section, Select, Switch, Table } from "../ui";
import { formatWhen } from "./shared";
import { confirmAction } from "../../context/ConfirmContext";
import { navigate } from "../../router";
import { settingsPath } from "../../navigation";
import { FULL_OPERATOR_CAPABILITIES, PUBLIC_OPERATOR_CAPABILITIES } from "./operatorCapabilities";
import styles from "./TelegramOperatorPanel.module.css";

/** What a linked chat hears about. No route means "on" (the gateway's default). */
export const TELEGRAM_EVENTS = [
  { eventClass: "review_waiting", label: "A review is waiting for you" },
  { eventClass: "run_failed", label: "A run failed" },
  { eventClass: "run_done", label: "A run finished" },
] as const;

const LINK_TTL_SECONDS = 600;
const LINK_POLL_MS = 2000;

function eventEnabled(routes: NotificationRoute[], eventClass: string): boolean {
  const route = routes.find((item) => item.event_class === eventClass);
  return !route || (route.enabled && route.intent !== "SILENT");
}

function StepNumber({ index, done }: { index: number; done: boolean }) {
  return <span className={`${styles.stepNumber}${done ? ` ${styles.stepNumberDone}` : ""}`} aria-hidden="true">{done ? "✓" : index}</span>;
}

export function TelegramOperatorPanel() {
  const { projectId, loading: projectLoading } = useProject();
  const [settings, setSettings] = useState<TelegramSettings | null>(null);
  const [connections, setConnections] = useState<InteractionConnection[] | null>(null);
  const [links, setLinks] = useState<InteractionLink[]>([]);
  const [routes, setRoutes] = useState<NotificationRoute[]>([]);
  const [operatorProfiles, setOperatorProfiles] = useState<OperatorProfile[]>([]);
  const [selectedOperatorProfileId, setSelectedOperatorProfileId] = useState("");
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [token, setToken] = useState("");
  const [linkToken, setLinkToken] = useState<InteractionLinkToken | null>(null);
  const [linkState, setLinkState] = useState<"idle" | "waiting" | "expired">("idle");
  const [qr, setQr] = useState("");
  const pollTimer = useRef<number | null>(null);

  const connection = (connections ?? []).find((item) => item.enabled) ?? (connections ?? [])[0] ?? null;
  const verified = Boolean(connection?.external_identity);
  const activeLinks = links.filter((link) => link.status === "active");
  const linked = activeLinks.length > 0;

  const loadAll = useCallback(async () => {
    if (projectLoading || !projectId) return;
    try {
      const [nextSettings, nextConnections, nextRoutes, nextProfiles] = await Promise.all([
        interactionApi.telegramSettings(),
        interactionApi.listConnections(projectId),
        interactionApi.listNotificationRoutes(projectId),
        operatorApi.listProfiles(projectId).catch(() => []),
      ]);
      const telegram = nextConnections.filter((item) => item.channel_type === "telegram");
      setSettings(nextSettings);
      setConnections(telegram);
      setRoutes(nextRoutes);
      setOperatorProfiles(nextProfiles);
      const current = telegram.find((item) => item.enabled) ?? telegram[0];
      setLinks(current ? await interactionApi.listLinks(current.id) : []);
      setSelectedOperatorProfileId(current?.operator_profile_id ?? "");
      setLoadError("");
    } catch (caught) {
      setConnections(null);
      setLoadError(describeApiError(caught, "Could not load Telegram settings."));
    }
  }, [projectId, projectLoading]);

  useEffect(() => { void loadAll(); }, [loadAll]);
  useEffect(() => () => { if (pollTimer.current !== null) window.clearTimeout(pollTimer.current); }, []);

  useEffect(() => {
    let cancelled = false;
    if (!linkToken?.deep_link) { setQr(""); return; }
    QRCode.toDataURL(linkToken.deep_link, { margin: 1, width: 288 })
      .then((url) => { if (!cancelled) setQr(url); })
      .catch(() => { if (!cancelled) setQr(""); });
    return () => { cancelled = true; };
  }, [linkToken?.deep_link]);

  async function run(key: string, action: () => Promise<void>, fallback: string) {
    setBusy(key); setError(""); setMessage("");
    try { await action(); }
    catch (caught) { setError(describeApiError(caught, fallback)); }
    finally { setBusy(""); }
  }

  async function connectBot() {
    const value = token.trim();
    if (!projectId) { setError("Choose a project first."); return; }
    if (!value) { setError("Paste the token BotFather gave you."); return; }
    await run("connect", async () => {
      const created = await interactionApi.createConnection({ project_id: projectId, name: "Telegram bot", bot_token: value });
      if (!created.bot_username) {
        try { await interactionApi.deleteConnection(created.id); } catch { /* the error below matters more */ }
        throw new Error("Telegram did not accept that token. Copy it again from BotFather and paste the whole line.");
      }
      setToken("");
      await loadAll();
      setMessage(`Connected to @${created.bot_username}.`);
    }, "Could not connect the bot.");
  }

  function watchLink(tokenId: string) {
    const deadline = Date.now() + LINK_TTL_SECONDS * 1000;
    const tick = async () => {
      try {
        const status = await interactionApi.linkTokenStatus(tokenId);
        if (status.linked) {
          setLinkToken(null); setLinkState("idle");
          await loadAll();
          setMessage(status.external_username ? `Linked to @${status.external_username}.` : "Your Telegram account is linked.");
          return;
        }
        if (status.expired || Date.now() > deadline) { setLinkState("expired"); return; }
      } catch {
        // A transient error keeps watching; the deadline still applies.
      }
      pollTimer.current = window.setTimeout(() => void tick(), LINK_POLL_MS);
    };
    pollTimer.current = window.setTimeout(() => void tick(), LINK_POLL_MS);
  }

  async function startLink() {
    if (!connection || !projectId) return;
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    await run("link", async () => {
      const created = await interactionApi.createLinkToken({ connection_id: connection.id, project_id: projectId, ttl_seconds: LINK_TTL_SECONDS });
      setLinkToken(created);
      setLinkState("waiting");
      watchLink(created.id);
    }, "Could not create a link.");
  }

  async function setEvent(eventClass: string, on: boolean) {
    if (!projectId) return;
    await run(`event-${eventClass}`, async () => {
      const saved = await interactionApi.upsertNotificationRoute({
        project_id: projectId, event_class: eventClass, intent: on ? "IMMEDIATE" : "SILENT",
        channels: [{ channel: "telegram" }], enabled: true,
      });
      setRoutes((current) => [...current.filter((item) => item.event_class !== eventClass), saved]);
    }, "Could not save that choice.");
  }

  async function sendTest() {
    if (!projectId) return;
    await run("test", async () => {
      const result = await interactionApi.sendTestNotification({
        project_id: projectId, event_class: "telegram_test", intent: "IMMEDIATE",
        title: "Lynewright test message", body: "Telegram is connected. You will get notifications here.",
      });
      const sent = (result.results ?? []).some((item) => item.status === "sent");
      if (!sent) {
        const detail = (result.results ?? []).map((item) => item.detail).filter(Boolean)[0];
        throw new Error(detail ? `Not delivered: ${detail}.` : "Not delivered. Link your Telegram account first.");
      }
      setMessage("Test message sent. Check Telegram.");
    }, "Could not send the test message.");
  }

  async function unlink(link: InteractionLink) {
    if (!(await confirmAction({ title: "Unlink this Telegram account?", message: "It stops receiving notifications.", confirmLabel: "Unlink", tone: "danger" }))) return;
    await run(`unlink-${link.id}`, async () => {
      await interactionApi.revokeLink(link.id);
      await loadAll();
    }, "Could not unlink the account.");
  }

  async function disconnect() {
    if (!connection) return;
    if (!(await confirmAction({
      title: "Disconnect Telegram?",
      message: "Lynewright stops listening to this bot and unlinks every account. You can connect again later.",
      confirmLabel: "Disconnect", tone: "danger",
    }))) return;
    await run("disconnect", async () => {
      await interactionApi.deleteConnection(connection.id);
      setLinkToken(null); setLinkState("idle");
      await loadAll();
      setMessage("Telegram is disconnected.");
    }, "Could not disconnect Telegram.");
  }

  async function switchDelivery(mode: "polling" | "webhook") {
    if (!connection) return;
    await run("delivery", async () => {
      await interactionApi.setDeliveryMode(connection.id, mode);
      await loadAll();
      setMessage(mode === "webhook" ? "Telegram now pushes messages to this server." : "Lynewright now checks Telegram for messages itself.");
    }, "Could not change how messages arrive.");
  }

  async function saveTelegramAgent() {
    if (!connection) return;
    const chosen = operatorProfiles.find((profile) => profile.id === selectedOperatorProfileId);
    await run("agent", async () => {
      await interactionApi.updateConnection(connection.id, { operator_profile_id: selectedOperatorProfileId });
      await loadAll();
      setMessage(chosen ? `Telegram messages will use the ${chosen.name} agent role.` : "Telegram will use the first enabled agent role for this project.");
    }, "Could not save the agent role.");
  }

  async function grantAllAgentAccess() {
    const chosen = operatorProfiles.find((profile) => profile.id === selectedOperatorProfileId) ?? operatorProfiles.find((profile) => profile.enabled);
    if (!chosen) { setError("Create or enable an Operator role in Settings, then Operator first."); return; }
    if (!(await confirmAction({
      title: `Grant all permissions to ${chosen.name}?`,
      message: `This enables all ${PUBLIC_OPERATOR_CAPABILITIES.length} public Operator capabilities for Telegram. Sensitive actions still require your approval in Telegram.`,
      confirmLabel: "Grant all permissions",
    }))) return;
    await run("agent-access", async () => {
      await operatorApi.updateProfile(chosen.id, { granted_capabilities: FULL_OPERATOR_CAPABILITIES });
      setSelectedOperatorProfileId(chosen.id);
      await loadAll();
      setMessage(`${chosen.name} now has all ${PUBLIC_OPERATOR_CAPABILITIES.length} public permissions. Sensitive actions still require approval.`);
    }, "Could not update the role.");
  }

  const feedback = (
    <>
      {message ? <Banner tone="info" onDismiss={() => setMessage("")}>{message}</Banner> : null}
      {error ? <Banner tone="danger" onDismiss={() => setError("")}>{error}</Banner> : null}
    </>
  );

  if (loadError) {
    return <Section title="Telegram"><Banner tone="danger">{loadError}</Banner><div className={styles.actions}><Button variant="secondary" onClick={() => void loadAll()}>Try again</Button></div></Section>;
  }
  if (connections === null || settings === null) {
    return <Section title="Telegram"><p className={styles.hint}>Loading Telegram settings…</p></Section>;
  }
  if (!settings.enabled) {
    return (
      <Section title="Telegram" description="Get reviews and alerts on your phone.">
        <Banner tone="warning" title="Telegram is turned off on this server">
          The administrator switched it off (LYNEWRIGHT_TELEGRAM_ENABLED=0). Remove that setting and restart to use Telegram.
        </Banner>
      </Section>
    );
  }

  const linkStep = (
    <>
      {!linkToken ? (
        <div className={styles.actions}>
          <Button variant={linked ? "secondary" : "primary"} onClick={() => void startLink()} disabled={busy === "link"}>
            {linked ? "Link another account" : "Link my Telegram"}
          </Button>
        </div>
      ) : (
        <div className={styles.linkBox}>
          <div className={styles.linkActions}>
            <Button variant="primary" onClick={() => window.open(linkToken.deep_link, "_blank", "noopener,noreferrer")} disabled={!linkToken.deep_link}>Open in Telegram</Button>
            <p className={styles.hint}>Then tap <strong>Start</strong> in the chat with your bot.</p>
            {linkState === "waiting" ? <p className={styles.waiting} role="status">Waiting for Telegram…</p> : null}
            {linkState === "expired" ? <p className={styles.error} role="alert">That link expired. <Button variant="ghost" size="sm" onClick={() => void startLink()}>Make a new link</Button></p> : null}
          </div>
          {qr ? <img className={styles.qr} src={qr} alt="QR code that opens your bot in Telegram" /> : null}
        </div>
      )}
    </>
  );

  if (!connection || !verified || !linked) {
    const step1Done = Boolean(connection && verified);
    return (
      <Section title="Telegram" description="Get reviews and alerts on your phone. It takes about a minute.">
        <div className={styles.panel}>
          {feedback}
          <ol className={styles.steps}>
            <li className={`${styles.step}${step1Done ? ` ${styles.stepDone}` : ""}`}>
              <div className={styles.stepHeader}><StepNumber index={1} done={step1Done} /><h3 className={styles.stepTitle}>{step1Done ? `Bot connected: ${connection?.external_identity}` : "Create your bot"}</h3></div>
              {!step1Done ? (
                <>
                  <ol className={styles.howTo}>
                    <li>Open <strong>@BotFather</strong> in Telegram.</li>
                    <li>Send <code>/newbot</code> and choose a name.</li>
                    <li>Copy the token it sends you and paste it here.</li>
                  </ol>
                  <form className={styles.tokenRow} onSubmit={(event) => { event.preventDefault(); void connectBot(); }}>
                    <FormField label="Bot token" htmlFor="telegram-bot-token">
                      <Input id="telegram-bot-token" name="telegram_bot_token" type="password" autoComplete="off" value={token}
                        placeholder="123456789:AA…" onChange={(event) => setToken(event.target.value)} />
                    </FormField>
                    <Button variant="primary" type="submit" disabled={busy === "connect"}>{busy === "connect" ? "Connecting…" : "Connect"}</Button>
                    <Button variant="secondary" type="button" onClick={() => window.open("https://t.me/BotFather", "_blank", "noopener,noreferrer")}>Open BotFather</Button>
                  </form>
                </>
              ) : null}
            </li>
            <li className={styles.step} aria-disabled={!step1Done}>
              <div className={styles.stepHeader}><StepNumber index={2} done={false} /><h3 className={styles.stepTitle}>Link your Telegram account</h3></div>
              {step1Done ? linkStep : <p className={styles.hint}>Available after step 1.</p>}
            </li>
            <li className={styles.step} aria-disabled>
              <div className={styles.stepHeader}><StepNumber index={3} done={false} /><h3 className={styles.stepTitle}>Choose what you hear about</h3></div>
              <p className={styles.hint}>Available after step 2.</p>
            </li>
          </ol>
          {connection ? <div className={styles.actions}><Button variant="ghost" size="sm" onClick={() => void disconnect()} disabled={busy === "disconnect"}>Start over</Button></div> : null}
        </div>
      </Section>
    );
  }

  const webhookMode = connection.delivery_mode === "webhook";
  return (
    <Section title="Telegram" description="Reviews and alerts reach your linked Telegram account.">
      <div className={styles.panel}>
        {feedback}
        <div className={styles.statusRow} role="status" aria-label="Telegram connection status">
          <div className={styles.statusIdentity}>
            <strong>{connection.external_identity}</strong>
            <small>
              {webhookMode ? "Telegram pushes messages to this server." : "Lynewright checks Telegram for messages."}
              {connection.last_update_at ? ` Last message ${formatWhen(connection.last_update_at)}.` : ""}
            </small>
          </div>
          <Badge tone={connection.last_error ? "attention" : "positive"}>{connection.last_error ? "Needs attention" : "Connected"}</Badge>
        </div>
        {connection.last_error ? <Banner tone="warning">{connection.last_error}</Banner> : null}

        <fieldset className={styles.choices}>
          <legend className={styles.stepTitle}>Tell me when</legend>
          {TELEGRAM_EVENTS.map((event) => (
            <Switch key={event.eventClass} name={`telegram-${event.eventClass}`} label={event.label}
              checked={eventEnabled(routes, event.eventClass)} disabled={busy === `event-${event.eventClass}`}
              onChange={(on) => void setEvent(event.eventClass, on)} />
          ))}
        </fieldset>
        <div className={styles.actions}>
          <Button variant="secondary" onClick={() => void sendTest()} disabled={busy === "test"}>{busy === "test" ? "Sending…" : "Send test message"}</Button>
        </div>

        <Table label="Linked Telegram accounts" rows={activeLinks} getRowKey={(link) => link.id} columns={[
          { key: "account", header: "Linked account", render: (link) => link.external_username ? `@${link.external_username}` : link.external_user_id },
          { key: "linked", header: "Since", render: (link) => formatWhen(link.created_at) },
        ]} rowActions={(link) => <Button variant="secondary" size="sm" disabled={busy === `unlink-${link.id}`} onClick={() => void unlink(link)}>Unlink</Button>} />
        {linkStep}

        <details className={styles.advanced}>
          <summary>Advanced</summary>
          <div className={styles.advancedBody}>
            <div className={styles.statusIdentity}>
              <strong>How messages arrive</strong>
              <small>
                {webhookMode
                  ? "Webhook: Telegram pushes messages to your public address. Lynewright registered it for you."
                  : "Polling: Lynewright asks Telegram for new messages. Works without a public address."}
              </small>
              <div className={styles.actions}>
                {webhookMode
                  ? <Button variant="secondary" size="sm" disabled={busy === "delivery"} onClick={() => void switchDelivery("polling")}>Use polling</Button>
                  : <Button variant="secondary" size="sm" disabled={busy === "delivery" || !settings.webhook_available} onClick={() => void switchDelivery("webhook")}>Use webhook (faster)</Button>}
              </div>
              {!webhookMode && !settings.webhook_available ? <small>Webhook needs LYNEWRIGHT_PUBLIC_URL to be a public https:// address.</small> : null}
            </div>
            <div className={styles.tokenRow}>
              <FormField label="Agent role for messages" htmlFor="telegram-agent-profile" helpText="The Operator role that answers messages sent to this bot.">
                <Select id="telegram-agent-profile" name="telegram_agent_profile" value={selectedOperatorProfileId} onChange={(event) => setSelectedOperatorProfileId(event.target.value)}>
                  <option value="">Automatic — first enabled role</option>
                  {operatorProfiles.map((profile) => (
                    <option key={profile.id} value={profile.id} disabled={!profile.enabled}>{profile.name}{profile.enabled ? "" : " (disabled)"}</option>
                  ))}
                </Select>
              </FormField>
              <Button variant="secondary" disabled={busy === "agent"} onClick={() => void saveTelegramAgent()}>Save role</Button>
            </div>
            <div className={styles.actions}>
              <Button variant="secondary" size="sm" disabled={busy === "agent-access" || operatorProfiles.length === 0} onClick={() => void grantAllAgentAccess()}>Grant all permissions</Button>
              <Button variant="secondary" size="sm" onClick={() => navigate(settingsPath("operator"))}>Edit role permissions</Button>
            </div>
            <div className={styles.actions}>
              <Button variant="danger" disabled={busy === "disconnect"} onClick={() => void disconnect()}>Disconnect Telegram</Button>
            </div>
          </div>
        </details>
      </div>
    </Section>
  );
}
