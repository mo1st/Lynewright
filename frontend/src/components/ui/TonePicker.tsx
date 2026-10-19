import { useEffect, useState } from "react";

/** Voice presets, expressed as the instruction each one adds.
 *
 * Deliberately phrased as *direction* rather than as a new capability: every
 * builder already threads free-text `instructions` to the provider, so a tone is
 * a prepended clause on that same string. No new backend surface, and the
 * operator can still override any of it in the prompt box underneath.
 */
export const TONES: ReadonlyArray<{ key: string; label: string; clause: string }> = [
  {
    key: "plain",
    label: "Plain and direct",
    clause: "Write plainly. Short sentences, concrete nouns, no metaphor and no hype.",
  },
  {
    key: "warm",
    label: "Warm and reassuring",
    clause: "Write warmly, in the second person, acknowledging the reader's hesitation before answering it.",
  },
  {
    key: "expert",
    label: "Expert and precise",
    clause: "Write with quiet authority: specific, unhurried, and explanatory rather than persuasive.",
  },
  {
    key: "bold",
    label: "Bold and punchy",
    clause: "Write with energy: strong verbs, short lines, and a clear point of view.",
  },
  {
    key: "editorial",
    label: "Editorial",
    clause: "Write like a magazine feature: an observation first, the product second, and no direct-response cadence.",
  },
];

/** Fold a chosen tone into the operator's own instruction text.
 *
 * The tone goes first so the operator's prose keeps recency over it, matching how
 * the backend already appends `instructions` last in the user prompt. An
 * unselected tone returns the instruction unchanged, so an untouched control
 * leaves the request — and therefore its artifact hash — exactly as before.
 */
export function composeInstructions(tone: string, instructions: string): string {
  const clause = TONES.find((entry) => entry.key === tone)?.clause;
  const text = instructions.trim();
  if (!clause) return text;
  return text ? `${clause}\n\n${text}` : clause;
}

/** Remember a tone selection across the phase unmounts the Lab performs. */
export function useTone(storageKey: string): [string, (value: string) => void] {
  const [value, setValue] = useState(() => read(storageKey));
  useEffect(() => { setValue(read(storageKey)); }, [storageKey]);
  return [value, (next: string) => {
    setValue(next);
    try { window.localStorage.setItem(storageKey, next); } catch { /* not persisted */ }
  }];
}

function read(storageKey: string): string {
  try { return window.localStorage.getItem(storageKey) ?? ""; } catch { return ""; }
}

export function TonePicker({
  value, onChange, disabled = false, label = "Voice",
}: {
  value: string;
  onChange: (tone: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  return <label className="tone-picker">
    <span>{label} <small>Optional</small></span>
    <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      <option value="">Default voice</option>
      {TONES.map((tone) => <option key={tone.key} value={tone.key}>{tone.label}</option>)}
    </select>
  </label>;
}
