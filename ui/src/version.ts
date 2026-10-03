export const RELOAD_KEY = "ccmc.reload";

export type VersionDecision = "none" | "reload" | "manual";

interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
  removeItem(k: string): void;
}

/**
 * Decide what to do when the server announces its version.
 *
 * - Same version (or none announced): nothing, and forget any earlier reload marker.
 * - Different version: reload once, remembering the `build->server` pair in sessionStorage.
 * - Different version and that pair was already tried (or storage is unusable, so a loop could
 *   not be prevented): show the banner with a manual Reload button instead.
 *
 * The function has a side effect (it records the pair), so it is not idempotent: calling it twice
 * for the same mismatch returns "reload" then "manual". React.StrictMode double-invokes effects in
 * development, which therefore shows the manual banner there; production calls it once.
 */
export function decideReload(build: string, server: string | null | undefined, storage: StorageLike | null): VersionDecision {
  if (!server || server === build) {
    try {
      storage?.removeItem(RELOAD_KEY);
    } catch {
      /* ignore */
    }
    return "none";
  }
  const pair = `${build}->${server}`;
  if (!storage) return "manual";
  try {
    if (storage.getItem(RELOAD_KEY) === pair) return "manual";
    storage.setItem(RELOAD_KEY, pair);
    return "reload";
  } catch {
    return "manual";
  }
}

export function sessionStore(): StorageLike | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}
