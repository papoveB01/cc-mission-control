import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ActivityFeed } from "./components/ActivityFeed";
import { AgentLane, LaneLabels } from "./components/AgentLane";
import { AgentModal } from "./components/AgentModal";
import { CallDrawer } from "./components/CallDrawer";
import { EmptyState } from "./components/EmptyState";
import { Header } from "./components/Header";
import { ContextPanel } from "./components/ContextPanel";
import { Dock } from "./components/Dock";
import { Panel } from "./components/Panel";
import { Telemetry } from "./components/Telemetry";
import { Topology } from "./components/Topology";
import { scrollBehavior } from "./motion";
import { historyKey, laneNames, liveAgentIds, orderSessions, resolveSelected } from "./store";
import type { HistoryPoint } from "./store";
import type { ToolCall } from "./types";
import { useMissionSocket } from "./useMissionSocket";

const NO_HISTORY: readonly HistoryPoint[] = [];

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
  const [modalAgent, setModalAgent] = useState<string | null>(null);
  const modalOpen = useRef(false);
  const trigger = useRef<Element | null>(null);
  const skipReturn = useRef(false);
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
    setModalAgent(null);
  }, []);

  const sid = selectedId;
  const onOpenCall = useCallback(
    (call: ToolCall) => {
      if (sid) setOpen({ sessionId: sid, callId: call.id, tool: call.tool });
    },
    [sid],
  );
  const onClose = useCallback(() => setOpen(null), []);

  const onOpenAgent = useCallback((agentId: string, el?: Element) => {
    if (!modalOpen.current) trigger.current = el ?? document.activeElement;
    modalOpen.current = true;
    setModalAgent(agentId);
  }, []);
  const onCloseModal = useCallback(() => {
    modalOpen.current = false;
    setModalAgent(null);
  }, []);

  /** "Show lane": close the modal, scroll to the lane and replay the highlight. */
  const onShowLane = useCallback((agentId: string) => {
    skipReturn.current = true;
    modalOpen.current = false;
    setModalAgent(null);
    setHighlight(null);
    requestAnimationFrame(() => {
      setHighlight(agentId);
      const lane = document.getElementById(`lane-${agentId}`);
      lane?.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
      lane?.querySelector<HTMLElement>(".lane-label")?.focus({ preventScroll: true });
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
  const modalWrap = useRef<HTMLDivElement>(null);
  // Background is inert behind any overlay; the modal itself is inert while the call drawer is above it.
  useLayoutEffect(() => {
    if (appRoot.current) appRoot.current.inert = open !== null || modalAgent !== null;
    if (modalWrap.current) modalWrap.current.inert = open !== null;
  }, [open, modalAgent]);

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
      {session ? <Telemetry session={session} /> : null}
      <main
        id="session-panel"
        role={session ? "tabpanel" : undefined}
        aria-labelledby={session ? `tab-${session.id}` : undefined}
        className="main"
      >
        {session ? (
          <div className="body">
            <div className="col-side">
              <Panel title="Topology" id="topology-panel">
                <Topology key={session.id} agents={session.agents} names={names} selected={modalAgent} epoch={state.snapshots} onOpenAgent={onOpenAgent} />
              </Panel>
              <Panel title="Context" id="context-panel">
                <ContextPanel sessionId={session.id} agents={session.agents} names={names} history={state.history} onOpenAgent={onOpenAgent} />
              </Panel>
            </div>
            <LaneLabels.Provider value={names}>
              <div className="lanes">
                {session.agents.map((a) => (
                  <AgentLane
                    key={a.id}
                    agent={a}
                    sessionEnded={session.status === "ended" ? (session.ended ?? session.last_event) : null}
                    highlighted={highlight === a.id}
                    onOpenCall={onOpenCall}
                    onOpenAgent={onOpenAgent}
                  />
                ))}
              </div>
            </LaneLabels.Provider>
            <ActivityFeed activity={session.activity} names={names} liveAgents={liveAgents} onOpenAgent={onOpenAgent} />
            <Dock />
          </div>
        ) : (
          <EmptyState />
        )}
      </main>
      </div>
      {modalAgent !== null && session ? (
        <div ref={modalWrap}>
          <AgentModal
            session={session}
            agentId={modalAgent}
            names={names}
            history={state.history.get(historyKey(session.id, modalAgent)) ?? NO_HISTORY}
            suspended={open !== null}
            returnTo={trigger.current}
            skipReturn={skipReturn}
            onClose={onCloseModal}
            onShowLane={onShowLane}
            onOpenAgent={onOpenAgent}
            onOpenCall={onOpenCall}
          />
        </div>
      ) : null}
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
