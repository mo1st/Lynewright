import { Moon, Sun } from "lucide-react";
import { useTheme } from "../../context/ThemeContext";
import styles from "./design-system.module.css";

export function ThemeToggle({ mode = "cycle" }: { mode?: "cycle" | "options" }) {
  const { preference, resolvedTheme, setPreference } = useTheme();
  if (mode === "options") return <div className={styles.themeOptions} role="group" aria-label="Theme">
    {(["light", "dark", "system"] as const).map((option) => <button key={option} type="button" className={`${styles.themeOption} ui-theme-option${preference === option ? ` ${styles.themeOptionActive}` : ""}`} aria-pressed={preference === option} onClick={() => setPreference(option)}>{option === "light" ? <Sun size={15} aria-hidden="true" /> : option === "dark" ? <Moon size={15} aria-hidden="true" /> : null}<span>{option[0].toUpperCase() + option.slice(1)}</span></button>)}
  </div>;
  const next = preference === "system" ? "light" : preference === "light" ? "dark" : "system";
  const label = `Theme: ${preference}. Switch to ${next} theme.`;
  return <button type="button" className={`${styles.themeToggle} ui-theme-toggle`} aria-label={label} title={label} onClick={() => setPreference(next)}>{resolvedTheme === "dark" ? <Moon size={16} aria-hidden="true" /> : <Sun size={16} aria-hidden="true" />}<span>{preference === "system" ? "System" : preference === "dark" ? "Dark" : "Light"}</span></button>;
}
