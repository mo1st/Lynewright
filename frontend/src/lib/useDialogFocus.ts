import { type RefObject, useEffect } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type FocusManagementOptions = {
  /** Inline disclosures need focus return but must not trap the whole page. */
  trap?: boolean;
  /** A deterministic first target, such as a dialog's safe action or close button. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Used when the element that opened a dialog was removed during the action. */
  restoreFocusRef?: RefObject<HTMLElement | null>;
};

export function useDialogFocus(
  open: boolean,
  containerRef: RefObject<HTMLElement | null>,
  onClose: () => void,
  options: FocusManagementOptions = {},
): void {
  const { trap = true, initialFocusRef, restoreFocusRef } = options;

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const container = containerRef.current;
    const focusable = () => Array.from(container?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    let active = true;

    queueMicrotask(() => {
      if (!active) return;
      const initial = initialFocusRef?.current;
      if (initial && container?.contains(initial) && !initial.matches(":disabled")) {
        initial.focus();
      } else {
        focusable()[0]?.focus();
      }
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (!trap || event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        container?.focus();
        return;
      }
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      active = false;
      document.removeEventListener("keydown", handleKeyDown);
      const restore = previouslyFocused
        && previouslyFocused !== document.body
        && previouslyFocused.isConnected
        && !previouslyFocused.matches(":disabled")
        ? previouslyFocused
        : restoreFocusRef?.current;
      if (restore?.isConnected && !restore.matches(":disabled")) restore.focus();
    };
  }, [containerRef, initialFocusRef, onClose, open, restoreFocusRef, trap]);
}
