import { useState, type KeyboardEvent, type ReactNode } from "react";
import { Check, Copy, PackageOpen } from "lucide-react";
import { statusTone, type StatusTone } from "../../lib/status";
import styles from "./design-system.module.css";

export type BadgeTone = "success" | "warning" | "danger" | "info" | "neutral" | StatusTone;
const badgeClass: Record<BadgeTone, string> = { success: styles.badgeSuccess, positive: styles.badgeSuccess, warning: styles.badgeWarning, attention: styles.badgeWarning, danger: styles.badgeDanger, critical: styles.badgeDanger, info: styles.badgeInfo, active: styles.badgeInfo, neutral: styles.badgeNeutral };

export function Badge({ tone = "neutral", children, className = "" }: { tone?: BadgeTone; children: ReactNode; className?: string }) {
  return <span className={`${styles.badge} ${badgeClass[tone]} ${className}`.trim()}>{children}</span>;
}

export function StatusBadge({ status, tone, children }: { status?: string | null; tone?: "neutral" | "success" | "warning" | "danger" | "info"; children?: ReactNode }) {
  const mappedTone: BadgeTone = status ? statusTone(status) : tone ?? "neutral";
  return <Badge tone={mappedTone}>{children ?? (status ? status.replaceAll("_", " ") : "Unknown")}</Badge>;
}

export type TableColumn<T> = { key: string; header: ReactNode; render: (row: T) => ReactNode; className?: string };
export function Table<T>({ columns, rows, getRowKey, loading = false, empty = "No rows to show.", rowActions, label, onRowClick }: { columns: Array<TableColumn<T>>; rows: T[]; getRowKey: (row: T) => string | number; loading?: boolean; empty?: ReactNode; rowActions?: (row: T) => ReactNode; label?: string; onRowClick?: (row: T) => void }) {
  const activateRow = (event: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if ((event.target as HTMLElement).closest("a,button,input,select,textarea,[role=button]")) return;
    event.preventDefault();
    onRowClick?.(row);
  };
  return <div className={styles.tableWrap} data-table-wrap role="region" aria-label={label ? `${label} table` : "Scrollable table"} tabIndex={0}><table className={styles.table} aria-label={label}><thead><tr>{columns.map((column) => <th key={column.key} scope="col" className={column.className}>{column.header}</th>)}{rowActions ? <th scope="col" className={styles.tableActions} data-table-actions>Actions</th> : null}</tr></thead><tbody>{loading ? <tr><td className={styles.tableEmpty} colSpan={columns.length + Number(Boolean(rowActions))}>Loading…</td></tr> : rows.length ? rows.map((row) => <tr className={`${styles.tableRow}${onRowClick ? " ui-table-row-clickable" : ""}`} key={getRowKey(row)} tabIndex={onRowClick ? 0 : undefined} onClick={(event) => { if (onRowClick && !(event.target as HTMLElement).closest("a,button,input,select,textarea,[role=button]")) onRowClick(row); }} onKeyDown={onRowClick ? (event) => activateRow(event, row) : undefined}>{columns.map((column) => {
    const content = column.render(row);
    const text = typeof content === "string" || typeof content === "number" ? String(content) : undefined;
    return <td key={column.key} className={`${styles.tableCell} ${column.className ?? ""}`.trim()} title={text}>{text === undefined ? content : <span className={styles.tableCellText} data-table-cell-text>{content}</span>}</td>;
  })}{rowActions ? <td className={styles.tableActions} data-table-actions>{rowActions(row)}</td> : null}</tr>) : <tr><td className={styles.tableEmpty} colSpan={columns.length + Number(Boolean(rowActions))}>{empty}</td></tr>}</tbody></table></div>;
}

export function KeyValue({ items, className = "" }: { items: Array<{ label: ReactNode; value: ReactNode }>; className?: string }) {
  return <dl className={`${styles.keyValue} ${className}`.trim()}>{items.map((item, index) => <div key={`${String(item.label)}-${index}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>;
}

export function CopyField({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try { await navigator.clipboard.writeText(value); setCopied(true); window.setTimeout(() => setCopied(false), 1200); } catch { setCopied(false); }
  }
  return <div className={`${styles.copyField} ${className}`.trim()}><span className={styles.copyLabel}>{label}</span><div className={styles.copyControl}><code className={styles.copyValue} title={value}>{value}</code><button type="button" className={styles.copyButton} aria-label={`Copy ${label}`} onClick={() => void copy()}>{copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}</button></div><span className="sr-only" aria-live="polite">{copied ? "Copied" : ""}</span></div>;
}

export function EmptyState({ title, description, icon, action, className = "" }: { title: ReactNode; description: ReactNode; icon?: ReactNode; action?: ReactNode; className?: string }) {
  return <div className={`${styles.emptyState} ${className}`.trim()}><span className={styles.emptyIcon} aria-hidden="true">{icon ?? <PackageOpen size={20} />}</span><h2 className={styles.emptyTitle}>{title}</h2><p className={styles.emptyDescription}>{description}</p>{action}</div>;
}
