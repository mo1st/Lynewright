import { useCallback, useEffect, useRef, useState } from "react";
import { IconButton } from "./Button";

/** How long a success message stays before it retires itself. */
const SUCCESS_TTL_MS = 6000;

export type AppMessageTone = "status" | "error";

type Message = { text: string; token: number };

/**
 * A single app-level message that expires on its own.
 *
 * The workspace used to hold `notice` and `error` as two plain strings that
 * roughly twenty unrelated handlers wrote into. Nothing ever cleared them, so a
 * message raised by one action outlived the action, the view, and often the
 * user's memory of causing it: remove an integration, walk to /settings,
 * and "Integration removed." was still sitting under the header. Errors were worse
 * — a background reload could overwrite the failure the user was reading.
 *
 * The setter keeps the `(string | null)` shape the call sites already pass, so
 * this is a change of behaviour, not of API.
 */
export function useAppMessage(tone: AppMessageTone) {
  const [message, setMessage] = useState<Message | null>(null);
  const timer = useRef<number | null>(null);
  const counter = useRef(0);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const set = useCallback((text: string | null) => {
    clearTimer();
    if (text === null) {
      setMessage(null);
      return;
    }
    counter.current += 1;
    setMessage({ text, token: counter.current });
    // Errors are the user's to dismiss: something failed, and a message that
    // vanishes on a timer is a failure they never got to read. Confirmations
    // have already done their job by the time they are read.
    if (tone === "status") {
      timer.current = window.setTimeout(() => setMessage(null), SUCCESS_TTL_MS);
    }
  }, [clearTimer, tone]);

  useEffect(() => clearTimer, [clearTimer]);

  return [message, set] as const;
}

type AppMessageProps = {
  message: { text: string; token: number } | null;
  tone: AppMessageTone;
  onDismiss: () => void;
};

export function AppMessage({ message, tone, onDismiss }: AppMessageProps) {
  if (!message) return null;
  const className = tone === "error" ? "error-message app-message" : "status-message app-message";
  return <div
    className={className}
    // Keyed by token so re-raising the same text still re-announces: two failed
    // saves in a row are two events, and a screen reader that stays silent for
    // the second one reports success by omission.
    key={message.token}
    role={tone === "error" ? "alert" : "status"}
    aria-live={tone === "error" ? "assertive" : "polite"}
  >
    <span className="app-message__text">{message.text}</span>
    <IconButton label="Dismiss message" className="app-message__dismiss" type="button" onClick={onDismiss}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
    </IconButton>
  </div>;
}
