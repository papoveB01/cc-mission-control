import { useSyncExternalStore } from "react";

/** One shared 1 s ticker for every live timer. Runs only while something subscribes. */
let now = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(fn: () => void): () => void {
  if (listeners.size === 0) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      listeners.forEach((l) => l());
    }, 1000);
  }
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = (): number => now;

/** Current time in epoch milliseconds, updated once per second. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

const noopSubscribe = (): (() => void) => () => {};

/** Like useNow, but only subscribes (and ticks) while `active`. Idle views cost nothing. */
export function useNowWhen(active: boolean): number {
  return useSyncExternalStore(active ? subscribe : noopSubscribe, getSnapshot, getSnapshot);
}
