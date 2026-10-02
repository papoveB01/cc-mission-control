import { memo } from "react";
import { useNow } from "../clock";
import { formatClock, formatDuration } from "../format";
import type { Session } from "../types";
import { AnimatedNumber } from "./AnimatedNumber";
import { Elapsed } from "./Elapsed";

function LastEvent({ at }: { at: number }) {
  const now = useNow();
  return <span className="tnum">{formatDuration(Math.max(0, now - at * 1000))} ago</span>;
}

function Cell({ label, children, tone }: { label: string; children: React.ReactNode; tone?: "err" }) {
  return (
    <div className={`tele-cell${tone ? ` tone-${tone}` : ""}`}>
      <span className="hud-label">{label}</span>
      <span className="tele-value">{children}</span>
    </div>
  );
}

export const Telemetry = memo(function Telemetry({ session }: { session: Session }) {
  const running = session.agents.filter((a) => a.status === "running").length;
  const calls = session.agents.reduce((n, a) => n + a.total_calls, 0);
  const errors = session.agents.reduce((n, a) => n + a.errors, 0);
  return (
    <section className="telemetry" aria-label="Session telemetry">
      <Cell label="Project">{session.title}</Cell>
      <Cell label="Model">{session.model ?? "unknown"}</Cell>
      <Cell label="Started">
        <span className="tnum">{formatClock(session.started)}</span>
      </Cell>
      <Cell label="Uptime">
        <Elapsed start={session.started} end={session.status === "ended" ? (session.ended ?? session.last_event) : null} />
      </Cell>
      <Cell label="Running">
        <AnimatedNumber value={running} />
      </Cell>
      <Cell label="Calls">
        <AnimatedNumber value={calls} />
      </Cell>
      <Cell label="Errors" tone={errors > 0 ? "err" : undefined}>
        <AnimatedNumber value={errors} />
      </Cell>
      <Cell label="Compactions">
        <AnimatedNumber value={session.compactions} />
      </Cell>
      <Cell label="Last event">{session.status === "ended" ? "Ended" : <LastEvent at={session.last_event} />}</Cell>
    </section>
  );
});
