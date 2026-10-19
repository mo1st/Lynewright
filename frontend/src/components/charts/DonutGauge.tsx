import type { CSSProperties } from "react";
export function DonutGauge({ value, label }: { value: number; label: string }) { const safe = Math.max(0, Math.min(100, value)); return <div className="ui-donut" role="img" aria-label={`${label}: ${safe}%`} style={{ "--gauge-value": `${safe}%` } as CSSProperties}><strong>{safe}%</strong><span>{label}</span></div>; }
