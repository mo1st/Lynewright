import type { ReactNode } from "react";

export function ChartFrame({ title, description, children, table }: { title: string; description: string; children: ReactNode; table?: ReactNode }) {
  return <figure className="ui-chart"><figcaption><strong>{title}</strong><span>{description}</span></figcaption>{children}{table ? <details className="ui-chart__table"><summary>View chart data</summary>{table}</details> : null}</figure>;
}
