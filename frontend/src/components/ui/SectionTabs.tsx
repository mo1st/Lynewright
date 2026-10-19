import { Tabs, type TabOption } from "./Tabs";
export type SectionTab<T extends string> = TabOption<T>;
export function SectionTabs<T extends string>({ tabs, active, onChange, ariaLabel, className = "" }: { tabs: Array<SectionTab<T>>; active: T; onChange: (key: T) => void; size?: "sm" | "md"; ariaLabel: string; className?: string; mobileLayout?: "scroll" | "grid" }) {
  return <Tabs tabs={tabs} active={active} onChange={onChange} label={ariaLabel} className={className} />;
}
