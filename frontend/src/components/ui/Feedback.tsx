import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, TriangleAlert, X } from "lucide-react";
import { useDialogFocus } from "../../lib/useDialogFocus";
import { IconButton } from "./Button";
import styles from "./design-system.module.css";

type Tone = "info" | "warning" | "danger";
const toneIcon = { info: Info, warning: TriangleAlert, danger: AlertCircle } as const;
export function Banner({ tone = "info", title, children, onDismiss, className = "" }: { tone?: Tone; title?: ReactNode; children: ReactNode; onDismiss?: () => void; className?: string }) {
  const Icon = toneIcon[tone];
  const toneClass = tone === "info" ? styles.bannerInfo : tone === "warning" ? styles.bannerWarning : styles.bannerDanger;
  return <div className={`${styles.banner} ${toneClass} ${className}`.trim()} role={tone === "danger" ? "alert" : "status"}><span className={styles.bannerIcon} aria-hidden="true"><Icon size={18} /></span><div className={styles.bannerContent}>{title ? <strong className={styles.bannerTitle}>{title}</strong> : null}<div className={styles.bannerMessage}>{children}</div></div>{onDismiss ? <IconButton className={styles.dismissButton} label="Dismiss message" onClick={onDismiss}><X size={16} /></IconButton> : null}</div>;
}

export function SystemBar({ storageKey, tone = "warning", title, children, className = "" }: { storageKey: string; tone?: Tone; title?: ReactNode; children: ReactNode; className?: string }) {
  const [dismissed, setDismissed] = useState(() => { try { return window.sessionStorage.getItem(`lynewright.system-bar:${storageKey}`) === "dismissed"; } catch { return false; } });
  if (dismissed) return null;
  return <Banner tone={tone} title={title} className={`${styles.systemBar} ${className}`.trim()} onDismiss={() => { try { window.sessionStorage.setItem(`lynewright.system-bar:${storageKey}`, "dismissed"); } finally { setDismissed(true); } }}>{children}</Banner>;
}

export function Dialog({ open, title, description, children, footer, onClose, labelledBy = "ui-dialog-title", preview = false, className = "", backdropClassName = "" }: { open: boolean; title: ReactNode; description?: ReactNode; children: ReactNode; footer?: ReactNode; onClose: () => void; labelledBy?: string; preview?: boolean; className?: string; backdropClassName?: string }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(open && !preview, dialogRef, onClose);
  if (!open) return null;
  return <div className={`${styles.dialogBackdrop}${preview ? ` ${styles.dialogPreviewBackdrop}` : ""} ${backdropClassName}`.trim()} onMouseDown={preview ? undefined : onClose}><div ref={dialogRef} className={`${styles.dialog}${preview ? ` ${styles.dialogPreview}` : ""} ${className}`.trim()} role="dialog" aria-modal={preview ? undefined : "true"} aria-labelledby={labelledBy} aria-describedby={description ? `${labelledBy}-description` : undefined} tabIndex={-1} onMouseDown={(event) => event.stopPropagation()}><header className={styles.dialogHeader}><div><h2 className={styles.dialogTitle} id={labelledBy}>{title}</h2>{description ? <p className={styles.pageDescription} id={`${labelledBy}-description`}>{description}</p> : null}</div><IconButton label="Close dialog" onClick={onClose}><X size={18} /></IconButton></header><div className={styles.dialogBody}>{children}</div>{footer ? <footer className={styles.dialogFooter}>{footer}</footer> : null}</div></div>;
}

export type ToastMessage = { id?: string; tone?: "success" | "info" | "warning" | "danger"; message: ReactNode };
type ToastApi = { addToast: (toast: ToastMessage) => void };
const ToastContext = createContext<ToastApi>({ addToast: () => undefined });
export function Toast({ tone = "info", message, onDismiss }: ToastMessage & { onDismiss?: () => void }) {
  const Icon = tone === "success" ? CheckCircle2 : tone === "warning" ? TriangleAlert : tone === "danger" ? AlertCircle : Info;
  const badgeTone = tone === "success" ? styles.badgeSuccess : tone === "warning" ? styles.badgeWarning : tone === "danger" ? styles.badgeDanger : styles.badgeInfo;
  return <div className={styles.toast} role={tone === "danger" ? "alert" : "status"}><span className={badgeTone} aria-hidden="true"><Icon size={16} /></span><span className={styles.toastMessage}>{message}</span>{onDismiss ? <button className={styles.dismissButton} type="button" aria-label="Dismiss notification" onClick={onDismiss}><X size={16} /></button> : null}</div>;
}
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Array<ToastMessage & { id: string }>>([]);
  const nextId = useRef(0);
  const addToast = useCallback((toast: ToastMessage) => setItems((current) => [...current, { ...toast, id: toast.id ?? `toast-${++nextId.current}` }]), []);
  const dismiss = (id: string) => setItems((current) => current.filter((item) => item.id !== id));
  useEffect(() => {
    const timers = items.map((item) => window.setTimeout(() => dismiss(item.id), 5000));
    return () => timers.forEach(window.clearTimeout);
  }, [items]);
  return <ToastContext.Provider value={{ addToast }}>{children}<div className={styles.toastRegion} role="region" aria-label="Notifications" aria-live="polite">{items.map((item) => <Toast key={item.id} {...item} onDismiss={() => dismiss(item.id)} />)}</div></ToastContext.Provider>;
}
export function useToast(): ToastApi { return useContext(ToastContext); }
