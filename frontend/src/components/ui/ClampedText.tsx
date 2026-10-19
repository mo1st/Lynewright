import { useId, useState } from "react";

/**
 * Long remote text (tool descriptions, capability text, error detail) clamped
 * to `--clamp-lines` (default 2) with a disclosure control beside it, so the
 * full value is reachable without being displayed by default.
 *
 * The string stays whole in the DOM: clamping is visual only, so screen
 * readers keep reading it in full. Short values render bare with no toggle.
 */
export function ClampedText({ text, lines = 2, label }: {
  text: string;
  lines?: number;
  /** Accessible name for the toggle, e.g. "tool description". */
  label?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const toggleId = useId();
  if (!text) return null;
  // Below ~140 characters two lines already show everything; a toggle would
  // be chrome without a job.
  if (text.length < 140) return <span title={text}>{text}</span>;
  return <>
    <span
      id={toggleId}
      className="text-clamp text-clamp--expandable"
      // @ts-expect-error CSS custom property for the clamp depth.
      style={{ "--clamp-lines": lines }}
      data-expanded={expanded || undefined}
      title={expanded ? undefined : text}
    >{text}</span>
    <button
      type="button"
      className="clamp-toggle"
      aria-expanded={expanded}
      aria-controls={toggleId}
      onClick={() => setExpanded((open) => !open)}
    >{expanded ? "Show less" : "Show more"}{label ? <span className="sr-only"> {label}</span> : null}</button>
  </>;
}
