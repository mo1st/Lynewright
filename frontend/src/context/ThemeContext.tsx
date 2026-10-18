import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";
type ThemeContextValue = { preference: ThemePreference; resolvedTheme: "light" | "dark"; setPreference: (preference: ThemePreference) => void; setUserId: (userId: string | null) => void };
const themeStorageKey = (userId: string | null) => `lynewright_theme:${userId ?? "guest"}`;
const ThemeContext = createContext<ThemeContextValue>({ preference: "system", resolvedTheme: "light", setPreference: () => undefined, setUserId: () => undefined });

function readPreference(userId: string | null): ThemePreference {
  try {
    const value = window.localStorage.getItem(themeStorageKey(userId));
    return value === "light" || value === "dark" || value === "system" ? value : "system";
  } catch { return "system"; }
}
function systemTheme(): "light" | "dark" {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [userId, setUserId] = useState<string | null>(null);
  const [preference, setPreferenceState] = useState<ThemePreference>(() => readPreference(null));
  const [resolvedTheme, setResolvedTheme] = useState<"light" | "dark">(() => preference === "system" ? systemTheme() : preference);

  useEffect(() => { setPreferenceState(readPreference(userId)); }, [userId]);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const sync = () => setResolvedTheme(preference === "system" ? systemTheme() : preference);
    sync();
    media?.addEventListener?.("change", sync);
    return () => media?.removeEventListener?.("change", sync);
  }, [preference]);
  useEffect(() => { document.documentElement.dataset.theme = resolvedTheme; }, [resolvedTheme]);

  const setPreference = (next: ThemePreference) => {
    setPreferenceState(next);
    try { window.localStorage.setItem(themeStorageKey(userId), next); } catch { /* Storage is optional. */ }
  };
  const value = useMemo(() => ({ preference, resolvedTheme, setPreference, setUserId }), [preference, resolvedTheme, setPreference, setUserId, userId]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
export function useTheme(): ThemeContextValue { return useContext(ThemeContext); }
