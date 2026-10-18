import { useEffect, useRef, type ReactNode } from "react";
import { AlertCircle, LoaderCircle, PackageOpen, WifiOff, ShieldAlert } from "lucide-react";
import { Button } from "./Button";
import { EmptyState } from "./DataDisplay";
import styles from "./design-system.module.css";

type PageStateProps = { kind: "loading" | "empty" | "error" | "offline" | "permission"; title?: string; message: string; actionLabel?: string; onAction?: () => void; icon?: ReactNode; compact?: boolean; autoFocus?: boolean; headingLevel?: 2 | 3 | 4 };
const DEFAULT_TITLES: Record<PageStateProps["kind"], string> = { loading: "Loading", empty: "Nothing here yet", error: "This view could not load", offline: "You appear to be offline", permission: "Access is restricted" };
const DEFAULT_ICONS = { loading: LoaderCircle, empty: PackageOpen, error: AlertCircle, offline: WifiOff, permission: ShieldAlert } as const;
export function PageState({ kind, title, message, actionLabel, onAction, icon, compact = false, autoFocus = false, headingLevel = 2 }: PageStateProps) {
  const stateRef = useRef<HTMLElement>(null);
  const liveRole = kind === "error" || kind === "offline" ? "alert" : "status";
  const Heading = `h${headingLevel}` as const;
  useEffect(() => { if (autoFocus) queueMicrotask(() => stateRef.current?.focus()); }, [autoFocus, kind, message]);
  if (kind === "empty") return <section ref={autoFocus ? stateRef : undefined} role={liveRole} tabIndex={autoFocus ? -1 : undefined} className={`${styles.card} ${compact ? "is-compact" : ""}`}><EmptyState title={title ?? DEFAULT_TITLES.empty} description={message} icon={icon ?? <PackageOpen size={20} />} action={actionLabel && onAction ? <Button variant="secondary" onClick={onAction}>{actionLabel}</Button> : undefined} /></section>;
  const Icon = DEFAULT_ICONS[kind];
  return <section ref={autoFocus ? stateRef : undefined} className={`${styles.banner} ${styles.bannerInfo}`} role={liveRole} tabIndex={autoFocus ? -1 : undefined} aria-busy={kind === "loading" || undefined}><span className={styles.bannerIcon} aria-hidden="true">{icon ?? <Icon size={18} />}</span><div><Heading className={styles.sectionTitle}>{title ?? DEFAULT_TITLES[kind]}</Heading><p className={styles.bannerMessage}>{message}</p>{actionLabel && onAction ? <Button variant="secondary" onClick={onAction}>{actionLabel}</Button> : null}</div></section>;
}
