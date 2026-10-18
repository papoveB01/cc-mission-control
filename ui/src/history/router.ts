import { useSyncExternalStore } from "react";
import { parseHash, type Route } from "./route";

const listeners = new Set<() => void>();
const notify = (): void => listeners.forEach((l) => l());

function subscribe(fn: () => void): () => void {
  if (listeners.size === 0) {
    window.addEventListener("hashchange", notify);
    window.addEventListener("popstate", notify);
  }
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0) {
      window.removeEventListener("hashchange", notify);
      window.removeEventListener("popstate", notify);
    }
  };
}

const snapshot = (): string => window.location.hash;

/** The current hash. Parse it with `parseHash`; keep it a string so the snapshot is stable. */
export function useHash(): string {
  return useSyncExternalStore(subscribe, snapshot, () => "");
}

export function useRoute(): Route {
  return parseHash(useHash());
}

/** Go to a hash. `replace` rewrites the current entry (filter changes), otherwise back/forward steps through it. */
export function navigate(hash: string, replace = false): void {
  if (hash === window.location.hash) return;
  if (replace) {
    window.history.replaceState(null, "", hash);
    notify();
  } else {
    window.location.hash = hash;
  }
}
