import { useEffect, useState } from "react";

/** Keep a builder prompt across component unmounts. The caller supplies a
 * storage key so separate Lynewright workflows can retain their own prompt.
 */
export function useBuilderPrompt(storageKey: string): [string, (value: string) => void] {
  const [value, setValue] = useState(() => readPrompt(storageKey));
  useEffect(() => { setValue(readPrompt(storageKey)); }, [storageKey]);
  return [value, (next: string) => {
    setValue(next);
    // Storage is unavailable in private modes and some embedded browsers; a
    // prompt that cannot be remembered must still be usable in this session.
    try { window.localStorage.setItem(storageKey, next); } catch { /* not persisted */ }
  }];
}

function readPrompt(storageKey: string): string {
  try { return window.localStorage.getItem(storageKey) ?? ""; } catch { return ""; }
}

/** Maximum prompt size accepted by the Lynewright builder UI. */
export const BUILDER_PROMPT_LIMIT = 2000;

/** Free-text steering for an AI builder — tone, angle, structure, emphasis. */
export function BuilderPrompt({
  label = "Custom prompt", hint, placeholder, value, onChange, rows = 3, disabled = false,
}: {
  label?: string;
  hint?: string;
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  disabled?: boolean;
}) {
  const remaining = BUILDER_PROMPT_LIMIT - value.length;
  return <label className="builder-prompt">
    <span>{label} <small>Optional</small></span>
    <textarea
      rows={rows}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      maxLength={BUILDER_PROMPT_LIMIT}
      onChange={(event) => onChange(event.target.value)}
    />
    <div className="builder-prompt__foot">
      {hint ? <small className="builder-prompt__hint">{hint}</small> : <span />}
      {value.length > 0 ? (
        <small className="builder-prompt__count" aria-live="polite">
          {remaining === 0
            ? `Character limit reached (${BUILDER_PROMPT_LIMIT})`
            : `${value.length} / ${BUILDER_PROMPT_LIMIT}`}
        </small>
      ) : null}
    </div>
  </label>;
}
