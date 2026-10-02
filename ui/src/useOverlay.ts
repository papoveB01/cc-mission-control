import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";

export const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function trapTab(e: ReactKeyboardEvent, panel: HTMLElement | null): void {
  if (e.key !== "Tab" || !panel) return;
  const nodes = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((n) => !n.hasAttribute("disabled"));
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  if (!first || !last) return;
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === panel)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

/**
 * Shared overlay behaviour: focus moves into the panel, Esc closes (capture phase, so nothing
 * underneath also reacts), and focus returns to whatever opened it unless `skipReturn` is set.
 */
export function useOverlay(onClose: () => void, opts: { initialFocus?: RefObject<HTMLElement | null>; skipReturn?: { current: boolean }; returnTo?: Element | null } = {}): {
  panel: RefObject<HTMLDivElement>;
  onKeyDown: (e: ReactKeyboardEvent) => void;
} {
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(opts.returnTo ?? document.activeElement);
  const skip = opts.skipReturn;
  const initial = opts.initialFocus;

  useEffect(() => {
    (initial?.current ?? panel.current)?.focus();
    const target = opener.current;
    return () => {
      if (skip?.current) {
        skip.current = false;
        return;
      }
      if ((target instanceof HTMLElement || target instanceof SVGElement) && target.isConnected) target.focus();
    };
  }, [initial, skip]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return { panel, onKeyDown: (e) => trapTab(e, panel.current) };
}
