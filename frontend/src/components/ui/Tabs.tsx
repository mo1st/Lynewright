import { useRef, type KeyboardEvent, type ReactNode } from "react";
import styles from "./design-system.module.css";

export type TabOption<T extends string> = { key: T; label: ReactNode; count?: number; disabled?: boolean };
export function Tabs<T extends string>({ tabs, active, onChange, label, className = "" }: { tabs: Array<TabOption<T>>; active: T; onChange: (key: T) => void; label: string; className?: string }) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const enabled = tabs.map((tab, i) => !tab.disabled ? i : -1).filter((i) => i >= 0);
    if (!enabled.length) return;
    const position = enabled.indexOf(index);
    const next = event.key === "Home" ? enabled[0] : event.key === "End" ? enabled[enabled.length - 1] : enabled[(position + (event.key === "ArrowRight" ? 1 : -1) + enabled.length) % enabled.length];
    onChange(tabs[next].key);
    refs.current[next]?.focus();
  };
  return <div className={`${styles.tabs} ${className}`.trim()} role="tablist" aria-label={label}>{tabs.map((tab, index) => <button key={tab.key} ref={(node) => { refs.current[index] = node; }} type="button" role="tab" aria-selected={active === tab.key} tabIndex={active === tab.key ? 0 : -1} disabled={tab.disabled} className={`${styles.tab}${active === tab.key ? ` ${styles.tabActive}` : ""}`} onKeyDown={(event) => onKeyDown(event, index)} onClick={() => onChange(tab.key)}>{tab.label}{typeof tab.count === "number" ? <span className={styles.tabCount}>{tab.count}</span> : null}</button>)}</div>;
}
