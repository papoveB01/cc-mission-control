import { memo } from "react";
import { formatClock, plural } from "../format";
import type { Session } from "../types";

export const SessionSummary = memo(function SessionSummary({ session }: { session: Session }) {
  const running = session.agents.filter((a) => a.status === "running").length;
  const calls = session.agents.reduce((n, a) => n + a.total_calls, 0);
  const errors = session.agents.reduce((n, a) => n + a.errors, 0);
  return (
    <div className="summary">
      <span className="summary-title">{session.title}</span>
      {session.model ? <span className="mono">{session.model}</span> : null}
      <span>
        Started <span className="tnum">{formatClock(session.started)}</span>
      </span>
      <span className="tnum">{running} running</span>
      <span className="tnum">{plural(calls, "call")}</span>
      <span className={`tnum${errors > 0 ? " err-text" : ""}`}>{plural(errors, "error")}</span>
      <span className="tnum">{plural(session.compactions, "compaction")}</span>
      {session.status === "ended" ? <span className="chip">Ended</span> : null}
    </div>
  );
});
