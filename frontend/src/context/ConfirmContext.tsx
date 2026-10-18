import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "../components/ui/Button";
import { useDialogFocus } from "../lib/useDialogFocus";

export type ConfirmRequest = {
  /** Short question. Rendered as the dialog heading. */
  title: string;
  /** What the user is agreeing to, including what becomes unrecoverable. */
  message?: string;
  /** Verb-object label for the accepting button, e.g. "Remove integration". */
  confirmLabel?: string;
  cancelLabel?: string;
  /** `danger` marks a confirm-destroy action as destructive. */
  tone?: "danger" | "default";
};

type Pending = ConfirmRequest & { resolve: (value: boolean) => void };

/**
 * Set by the mounted provider. A module-level handle rather than a hook because
 * every caller is an imperative event handler that used to call
 * `window.confirm` mid-function; threading a hook through eleven files of
 * nested panels would have changed far more code than the behaviour warrants.
 */
let openConfirm: ((request: ConfirmRequest) => Promise<boolean>) | null = null;

/**
 * In-app replacement for `window.confirm`.
 *
 * `window.confirm` freezes the page, cannot say which of two open panels is
 * asking, renders in browser chrome no theme reaches, and offers exactly "OK"
 * and "Cancel" — so twenty-one destructive actions all asked the same
 * unlabelled question. This keeps the awaited-boolean shape the call sites rely
 * on while letting each one name its own verb.
 *
 * Resolves `false` when no provider is mounted: without a way to ask, the safe
 * answer to "may I delete this?" is no.
 */
export function confirmAction(request: ConfirmRequest): Promise<boolean> {
  if (!openConfirm) return Promise.resolve(false);
  return openConfirm(request);
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    openConfirm = (request) => new Promise<boolean>((resolve) => {
      setPending({ ...request, resolve });
    });
    return () => { openConfirm = null; };
  }, []);

  const settle = useCallback((value: boolean) => {
    setPending((current) => {
      current?.resolve(value);
      return null;
    });
  }, []);

  // Escape and backdrop both mean "I did not agree to this".
  const dismiss = useCallback(() => settle(false), [settle]);
  useDialogFocus(pending !== null, dialogRef, dismiss);

  return <>
    {children}
    {pending ? <div className="modal-backdrop" onMouseDown={dismiss}>
      <div
        ref={dialogRef}
        className="modal-card confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby={pending.message ? "confirm-dialog-message" : undefined}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id="confirm-dialog-title">{pending.title}</h2>
        {pending.message ? <p id="confirm-dialog-message">{pending.message}</p> : null}
        <div className="confirm-dialog__actions">
          {/* Cancel is first, so the focus trap lands on the safe choice. */}
          <Button variant="secondary" onClick={dismiss}>{pending.cancelLabel ?? "Cancel"}</Button>
          <Button
            variant={pending.tone === "danger" ? "danger" : "primary"}
            dangerConfirm={pending.tone === "danger"}
            onClick={() => settle(true)}
          >{pending.confirmLabel ?? "Confirm"}</Button>
        </div>
      </div>
    </div> : null}
  </>;
}
