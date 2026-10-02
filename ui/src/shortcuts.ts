export type ShortcutAction =
  | "palette"
  | "help"
  | "prev-session"
  | "next-session"
  | "topology"
  | "timeline"
  | "feed-filter"
  | "next-error"
  | "escape";

export interface KeyInfo {
  key: string;
  ctrl: boolean;
  meta: boolean;
  alt: boolean;
  /** Focus is in an input, textarea, select or contenteditable element. */
  typing: boolean;
  /** Auto-repeat of a held key. */
  repeat?: boolean;
  /** IME composition in progress (including keyCode 229). */
  composing?: boolean;
}

const MAP: Record<string, ShortcutAction> = {
  "?": "help",
  "[": "prev-session",
  "]": "next-session",
  g: "topology",
  t: "timeline",
  f: "feed-filter",
  e: "next-error",
  Escape: "escape",
};

/**
 * Pure shortcut dispatcher. Cmd/Ctrl-K always opens the palette; every other shortcut is
 * ignored while typing and whenever Cmd, Ctrl or Alt is held.
 */
export function dispatchKey(k: KeyInfo): ShortcutAction | null {
  if (k.composing || k.repeat) return null;
  const key = k.key.length === 1 ? k.key.toLowerCase() : k.key;
  if ((k.ctrl || k.meta) && !k.alt && key === "k") return "palette";
  if (k.typing || k.ctrl || k.meta || k.alt) return null;
  return MAP[key] ?? null;
}

export function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  return t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable;
}

export const SHORTCUT_HELP: { keys: string; label: string }[] = [
  { keys: "Ctrl K / Cmd K", label: "Command palette (press again to close)" },
  { keys: "?", label: "This sheet" },
  { keys: "[  ]", label: "Previous / next session" },
  { keys: "G", label: "Focus the topology" },
  { keys: "T", label: "Toggle the timeline" },
  { keys: "F", label: "Focus the feed filter" },
  { keys: "E", label: "Open the next error call" },
  { keys: "Esc", label: "Close the top overlay" },
];
