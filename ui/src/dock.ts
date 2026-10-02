export const DOCK_MIN = 120;
export const DOCK_DEFAULT = 200;
export const DOCK_KEY = "ccmc.dock";

export interface DockState {
  height: number;
  collapsed: boolean;
}

export const defaultDock: DockState = { height: DOCK_DEFAULT, collapsed: false };

/** Clamp to 120 px .. 50% of the viewport height. */
export function clampDock(height: number, viewportH: number): number {
  const max = Math.max(DOCK_MIN, Math.floor(viewportH * 0.5));
  return Math.round(Math.min(max, Math.max(DOCK_MIN, height)));
}

interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

export function parseDock(raw: string | null): DockState {
  if (!raw) return defaultDock;
  try {
    const v: unknown = JSON.parse(raw);
    if (typeof v !== "object" || v === null) return defaultDock;
    const r = v as Record<string, unknown>;
    return {
      height: typeof r.height === "number" && Number.isFinite(r.height) ? r.height : DOCK_DEFAULT,
      collapsed: r.collapsed === true,
    };
  } catch {
    return defaultDock;
  }
}

export function loadDock(storage: StorageLike | null): DockState {
  try {
    return parseDock(storage ? storage.getItem(DOCK_KEY) : null);
  } catch {
    return defaultDock;
  }
}

export function saveDock(state: DockState, storage: StorageLike | null): void {
  try {
    storage?.setItem(DOCK_KEY, JSON.stringify(state));
  } catch {
    /* ignore */
  }
}
