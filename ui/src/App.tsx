import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ActivityFeed } from "./components/ActivityFeed";
import { AgentLane, LaneLabels } from "./components/AgentLane";
import { CallDrawer } from "./components/CallDrawer";
import { EmptyState } from "./components/EmptyState";
import { Header } from "./components/Header";
import { SessionSummary } from "./components/SessionSummary";
import { laneNames, liveAgentIds, orderSessions, resolveSelected } from "./store";
import type { ToolCall } from "./types";
import { useMissionSocket } from "./useMissionSocket";

interface OpenCall {
  sessionId: string;
  callId: string;
  tool: string;
}

export function App() {
  const { state, connection } = useMissionSocket();
  const [explicit, setExplicit] = useState<string | null>(null);
  const [open, setOpen] = useState<OpenCall | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const orderKey = [...state.sessions.values()].map((s) => `${s.id}\u0000${s.status}\u0000${s.title}\u0000${s.started}\u0000${s.cwd}`).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- Header only reads the fields in orderKey
  const ordered = useMemo(() => orderSessions(state.sessions.values()), [orderKey]);
  const selectedId = resolveSelected(state.sessions, explicit);
  const session = selectedId ? (state.sessions.get(selectedId) ?? null) : null;

  const agents = session?.agents ?? [];
  const namesKey = agents.map((a) => `${a.id}\u0000${a.label}\u0000${a.started}`).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- names depend only on the fields in namesKey
  const names = useMemo(() => laneNames(agents), [namesKey]);

  const liveKey = agents.filter((a) => a.status === "running" || a.status === "waiting").map((a) => a.id).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- depends only on liveKey
  const liveAgents = useMemo(() => liveAgentIds(agents), [liveKey]);

  const onSelect = useCallback((id: string) => {
    setExplicit(id);
    setOpen(null);
  }, []);

  const sid = selectedId;
  const onOpenCall = useCallback(
    (call: ToolCall) => {
      if (sid) setOpen({ sessionId: sid, callId: call.id, tool: call.tool });
    },
    [sid],
  );
  const onClose = useCallback(() => setOpen(null), []);

  const onJump = useCallback((agentId: string) => {
    setHighlight(agentId);
    requestAnimationFrame(() => {
      const el = document.getElementById(`lane-${agentId}`);
      if (!el) return;
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
    });
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setHighlight(null), 1800);
  }, []);

  useEffect(
    () => () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    [],
  );

  const appRoot = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (appRoot.current) appRoot.current.inert = open !== null;
  }, [open]);

  const openStatus = useMemo(() => {
    if (!open || !session || session.id !== open.sessionId) return null;
    for (const a of session.agents) {
      const c = a.calls.find((x) => x.id === open.callId);
      if (c) return c.status;
    }
    return null;
  }, [open, session]);

  return (
    <>
      <div ref={appRoot}>
      <Header sessions={ordered} selectedId={selectedId} connection={connection} onSelect={onSelect} />
      <main
        id="session-panel"
        role={session ? "tabpanel" : undefined}
        aria-labelledby={session ? `tab-${session.id}` : undefined}
        className="main"
      >
        {session ? (
          <>
            <SessionSummary session={session} />
            <div className="layout">
              <LaneLabels.Provider value={names}>
                <div className="lanes">
                  {session.agents.map((a) => (
                    <AgentLane
                      key={a.id}
                      agent={a}
                      sessionEnded={session.status === "ended" ? (session.ended ?? session.last_event) : null}
                      highlighted={highlight === a.id}
                      onOpenCall={onOpenCall}
                      onJump={onJump}
                    />
                  ))}
                </div>
              </LaneLabels.Provider>
              <ActivityFeed activity={session.activity} names={names} liveAgents={liveAgents} />
            </div>
          </>
        ) : (
          <EmptyState />
        )}
      </main>
      </div>
      {open ? (
        <CallDrawer
          key={`${open.sessionId}/${open.callId}`}
          sessionId={open.sessionId}
          callId={open.callId}
          tool={open.tool}
          status={openStatus}
          onClose={onClose}
        />
      ) : null}
    </>
  );
}
