import { createContext, useContext, useEffect, useRef, useState } from "react";
import { AuthError } from "./api";

/** Called when any request is answered with 401: the app swaps to the login view. */
export const AuthContext = createContext<() => void>(() => {});

export type ApiState<T> =
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "error"; message: string };

/**
 * Run `load` whenever `key` changes (null = do nothing). The previous request is aborted.
 * A 401 asks the app for the login view instead of showing an error.
 */
export function useApi<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  // The key is stored with the result: for the render in which `key` has just changed, the old
  // result belongs to another key and must not be shown.
  const [held, setHeld] = useState<{ key: string | null; state: ApiState<T> }>({ key, state: { status: "loading" } });
  const requireLogin = useContext(AuthContext);
  const loader = useRef(load);
  loader.current = load;

  useEffect(() => {
    if (key === null) return;
    const ctl = new AbortController();
    setHeld({ key, state: { status: "loading" } });
    loader
      .current(ctl.signal)
      .then((data) => {
        if (!ctl.signal.aborted) setHeld({ key, state: { status: "ok", data } });
      })
      .catch((err: unknown) => {
        if (ctl.signal.aborted) return;
        if (err instanceof AuthError) return requireLogin();
        setHeld({ key, state: { status: "error", message: err instanceof Error ? err.message : "Request failed" } });
      });
    return () => ctl.abort();
  }, [key, requireLogin]);

  return held.key === key ? held.state : LOADING;
}

const LOADING = { status: "loading" } as const;
