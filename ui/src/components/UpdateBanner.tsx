import { useEffect, useState } from "react";
import { decideReload, sessionStore, type VersionDecision } from "../version";

const RELOAD_DELAY_MS = 800;

/** Shown when the server runs a different version than this page was built for. */
export function UpdateBanner({ serverVersion }: { serverVersion: string | null }) {
  const [decision, setDecision] = useState<VersionDecision>("none");

  // Note on React.StrictMode: in development it runs effects twice on mount. decideReload records
  // the "build->server" pair on its first call, so the second call answers "manual" and the manual
  // banner replaces "reloading…" (and the reload timer of the first run is cleared by its cleanup).
  // Production builds run the effect once, so the automatic reload happens as designed. This is
  // intentional: it can never produce a reload loop, only a manual button in dev.
  useEffect(() => {
    if (serverVersion === null) return;
    const d = decideReload(__UI_VERSION__, serverVersion, sessionStore());
    setDecision(d);
    if (d !== "reload") return;
    const t = setTimeout(() => window.location.reload(), RELOAD_DELAY_MS);
    return () => clearTimeout(t);
  }, [serverVersion]);

  if (decision === "none") return null;
  return (
    <div className="update-banner" role="status">
      {decision === "reload" ? (
        <span>Dashboard updated, reloading…</span>
      ) : (
        <>
          <span>
            The dashboard is out of date (page {__UI_VERSION__}, server {serverVersion}).
          </span>
          <button type="button" className="act-btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </>
      )}
    </div>
  );
}
