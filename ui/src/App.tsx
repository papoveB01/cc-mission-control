import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { ActivityFeed } from "./components/ActivityFeed";
import { AgentLane, LaneLabels } from "./components/AgentLane";
import { AgentModal } from "./components/AgentModal";
import { CallDrawer } from "./components/CallDrawer";
import { ContextPanel } from "./components/ContextPanel";
import { Dock } from "./components/Dock";
import { EmptyState } from "./components/EmptyState";
import { FeedFilters } from "./components/FeedFilters";
import { Header } from "./components/Header";
import { Palette } from "./components/Palette";
import { Panel } from "./components/Panel";
import { ShortcutsSheet } from "./components/ShortcutsSheet";
import { Telemetry } from "./components/Telemetry";
import { Timeline, type Reveal, type TimelineInfo } from "./components/Timeline";
import { Topology } from "./components/Topology";
import { defaultDock, loadDock, saveDock, type DockState } from "./dock";
import { loadFilters, noFilters, pruneFilters, saveFilters, safeStorage, type Filters } from "./filters";
import { scrollBehavior } from "./motion";
import type { PaletteContext, PaletteItem } from "./palette";
import { dispatchKey, isTypingTarget, type ShortcutAction } from "./shortcuts";
import { historyKey, laneNames, liveAgentIds, orderSessions, resolveSelected } from "./store";
import type { HistoryPoint } from "./store";
import { errorCalls, initialTimeline, nextError, timelineReducer } from "./timeline";
import type { ToolCall } from "./types";
import { useMissionSocket } from "./useMissionSocket";

const NO_HISTORY: readonly HistoryPoint[] = [];
const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

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
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [filters, setFilters] = useState<Filters>(() => loadFilters());
  const [dock, setDock] = useState<DockState>(() => loadDock(safeStorage()) ?? defaultDock);
  const [tl, tlDispatch] = useReducer(timelineReducer, initialTimeline);
  const [fitPending, setFitPending] = useState(false);
  const [reveal, setReveal] = useState<Reveal | null>(null);
  const [paletteCtx, setPaletteCtx] = useState<PaletteContext | null>(null);
  const tlInfo = useRef<TimelineInfo | null>(null);
  const revealFocus = useRef<string | null>(null);

  const modalOpen = useRef(false);
  const drawerOpen = useRef(false);
  const trigger = useRef<Element | null>(null);
  const callTrigger = useRef<Element | null>(null);
  const helpReturn = useRef<Element | null>(null);
  const modalSkip = useRef(false);
  const paletteSkip = useRef(false);
  const lastError = useRef<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => saveFilters(filters), [filters]);
  useEffect(() => saveDock(dock, safeStorage()), [dock]);

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

  const toolKey = [...new Set(agents.flatMap((a) => Object.keys(a.tool_counts)))].sort().join("\u0001");
  const toolOptions = useMemo(() => (toolKey ? toolKey.split("\u0001") : []), [toolKey]);
  const agentOptions = useMemo(() => agents.map((a) => ({ id: a.id, label: names.get(a.id) ?? a.label })), [names, namesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Saved filters may name agents or tools that this session lacks: ignore those, and say so.
  const { filters: activeFilters, pruned } = useMemo(
    () => pruneFilters(filters, new Set(agentOptions.map((a) => a.id)), new Set(toolOptions)),
    [filters, agentOptions, toolOptions],
  );

  // Overlays that are actually rendered drive inertness, scroll lock and Esc layering.
  const modalShown = modalAgent !== null && session !== null;
  const drawerShown = open !== null && session !== null && session.id === open.sessionId;
  const paletteShown = paletteOpen;
  const helpShown = helpOpen;
  const anyOverlay = modalShown || drawerShown || paletteShown || helpShown;

  // Leaving a session (tab switch, session gone) clears overlays tied to it.
  useEffect(() => {
    setOpen(null);
    setModalAgent(null);
    modalOpen.current = false;
    drawerOpen.current = false;
    // Timeline window and the error cursor belong to one session: start fresh (follow live, fit).
    tlDispatch({ type: "reset" });
    lastError.current = null;
    revealFocus.current = null;
    setReveal(null);
    setFitPending(false);
  }, [selectedId]);

  const onSelect = useCallback((id: string) => {
    setExplicit(id);
    setOpen(null);
    setModalAgent(null);
    modalOpen.current = false;
    drawerOpen.current = false;
  }, []);

  const sid = selectedId;
  const onOpenCall = useCallback(
    (call: ToolCall, el?: Element) => {
      if (!sid) return;
      if (!drawerOpen.current) callTrigger.current = el ?? document.activeElement;
      revealFocus.current = null;
      drawerOpen.current = true;
      setOpen({ sessionId: sid, callId: call.id, tool: call.tool });
    },
    [sid],
  );
  const onClose = useCallback(() => {
    drawerOpen.current = false;
    setOpen(null);
    const id = revealFocus.current;
    if (id) {
      // The `e` shortcut revealed this call's bar: focus it once the drawer has restored focus.
      requestAnimationFrame(() => document.querySelector<SVGGElement>(`.tl-bar[data-call-id="${CSS.escape(id)}"]`)?.focus());
    }
  }, []);

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
    modalSkip.current = true;
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
  const drawerWrap = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (appRoot.current) appRoot.current.inert = anyOverlay;
    if (modalWrap.current) modalWrap.current.inert = drawerShown || paletteShown || helpShown;
    if (drawerWrap.current) drawerWrap.current.inert = paletteShown || helpShown;
    document.documentElement.classList.toggle("scroll-locked", anyOverlay);
  }, [anyOverlay, modalShown, drawerShown, paletteShown, helpShown]);

  const openStatus = useMemo(() => {
    if (!open || !session || session.id !== open.sessionId) return null;
    for (const a of session.agents) {
      const c = a.calls.find((x) => x.id === open.callId);
      if (c) return c.status;
    }
    return null;
  }, [open, session]);

  // ---- timeline and dock -------------------------------------------------
  const toggleDock = useCallback(() => setDock((d) => ({ ...d, collapsed: !d.collapsed })), []);
  const toggleFollow = useCallback(() => {
    const info = tlInfo.current;
    if (tl.follow) tlDispatch({ type: "follow", on: false, view: info?.view });
    else tlDispatch({ type: "follow", on: true });
  }, [tl.follow]);
  const zoom = useCallback((factor: number) => {
    const info = tlInfo.current;
    if (info) tlDispatch({ type: "zoom", factor, anchor: 0.5, view: info.view, bounds: info.bounds });
  }, []);
  // Fit works while the dock is collapsed: expand it and apply the fit when the timeline mounts.
  const fit = useCallback(() => {
    setDock((d) => (d.collapsed ? { ...d, collapsed: false } : d));
    setFitPending(true);
  }, []);
  const onFitDone = useCallback(() => setFitPending(false), []);

  // ---- actions shared by the palette and shortcuts ------------------------
  const jumpNextError = useCallback(
    (el?: Element) => {
      if (!session) return;
      const next = nextError(errorCalls(session), lastError.current);
      if (!next) return;
      lastError.current = next.id;
      onOpenCall(next, el);
      if (!dock.collapsed) {
        revealFocus.current = next.id;
        setReveal({ callId: next.id, agentId: next.agent_id, started: next.started, nonce: Date.now() });
      }
    },
    [session, onOpenCall, dock.collapsed],
  );

  const runAction = useCallback(
    (act: ShortcutAction): void => {
      const idx = ordered.findIndex((s) => s.id === selectedId);
      switch (act) {
        case "help":
          helpReturn.current = document.activeElement;
          setHelpOpen(true);
          break;
        case "prev-session":
        case "next-session": {
          const n = ordered.length;
          const t = ordered[(idx + (act === "next-session" ? 1 : -1) + n) % n];
          if (n > 0 && t) onSelect(t.id);
          break;
        }
        case "topology": {
          const el = [...document.querySelectorAll<HTMLElement>(".topo-svg .node, .topo-compact .topo-item")].find((e) => e.getClientRects().length > 0);
          el?.focus();
          break;
        }
        case "timeline":
          toggleDock();
          break;
        case "feed-filter":
          document.getElementById("feed-filter-text")?.focus();
          break;
        case "next-error":
          jumpNextError();
          break;
        default:
          break;
      }
    },
    [ordered, selectedId, onSelect, toggleDock, jumpNextError],
  );

  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandler.current = (e) => {
    const act = dispatchKey({
      key: e.key, ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey, typing: isTypingTarget(e.target),
      repeat: e.repeat, composing: e.isComposing || e.keyCode === 229,
    });
    if (!act || act === "escape") return;
    if (act === "palette") {
      e.preventDefault();
      if (helpShown) return;
      if (!paletteShown) paletteSkip.current = false;
      setPaletteOpen((o) => !o);
      return;
    }
    if (paletteShown || helpShown) return;
    if ((modalShown || drawerShown) && act !== "help" && act !== "next-error") return;
    e.preventDefault();
    runAction(act);
  };
  useEffect(() => {
    const h = (e: KeyboardEvent): void => keyHandler.current(e);
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);

  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const runPaletteItem = useCallback(
    (item: PaletteItem, opener: Element | null) => {
      const t = item.target;
      const handoff = (): void => {
        paletteSkip.current = true; // the next overlay takes over focus return
      };
      setPaletteOpen(false);
      if (t.type === "session") onSelect(t.id);
      else if (t.type === "agent") {
        handoff();
        onOpenAgent(t.id, opener ?? undefined);
      } else if (t.type === "call") {
        handoff();
        onOpenCall(t.call, opener ?? undefined);
      } else {
        switch (t.id) {
          case "timeline":
            toggleDock();
            break;
          case "follow":
            setDock((d) => (d.collapsed ? { ...d, collapsed: false } : d));
            toggleFollow();
            break;
          case "fit":
            fit();
            break;
          case "clear-filters":
            setFilters(noFilters);
            break;
          case "next-error":
            handoff();
            jumpNextError(opener ?? undefined);
            break;
          case "shortcuts":
            handoff();
            helpReturn.current = opener;
            setHelpOpen(true);
            break;
        }
      }
    },
    [onSelect, onOpenAgent, onOpenCall, toggleDock, toggleFollow, fit, jumpNextError],
  );
  const closeHelp = useCallback(() => setHelpOpen(false), []);

  // The palette works on a snapshot taken when it opens, refreshed at most once a second, so
  // results do not jump under the cursor while updates arrive every 150 ms.
  const paletteSource = useRef({ sessions: state.sessions, selectedId, names });
  paletteSource.current = { sessions: state.sessions, selectedId, names };
  useEffect(() => {
    if (!paletteOpen) {
      setPaletteCtx(null);
      return;
    }
    const take = (): void => {
      const src = paletteSource.current;
      setPaletteCtx({ sessions: orderSessions(src.sessions.values()), selectedId: src.selectedId, names: src.names });
    };
    take();
    const t = setInterval(take, 1000);
    return () => clearInterval(t);
  }, [paletteOpen]);

  const headerActions = useMemo(
    () => (
      <button type="button" className="hdr-btn" onClick={() => setPaletteOpen(true)} aria-label={`Open command palette (${isMac ? "Cmd" : "Ctrl"} K)`} aria-haspopup="dialog">
        <span>Search</span> <kbd>{isMac ? "Cmd K" : "Ctrl K"}</kbd>
      </button>
    ),
    [],
  );

  return (
    <>
      <div ref={appRoot}>
        <Header sessions={ordered} selectedId={selectedId} connection={connection} onSelect={onSelect} actions={headerActions} />
        {session ? <Telemetry session={session} /> : null}
        <main
          id="session-panel"
          tabIndex={-1}
          role={session ? "tabpanel" : undefined}
          aria-labelledby={session ? `tab-${session.id}` : undefined}
          className="main"
        >
          {session ? (
            <div className="body">
              <div className="col-side">
                <Panel title="Topology" id="topology-panel">
                  <Topology key={session.id} agents={session.agents} names={names} selected={modalShown ? modalAgent : null} epoch={state.snapshots} onOpenAgent={onOpenAgent} />
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
                      filters={activeFilters}
                      onOpenCall={onOpenCall}
                      onOpenAgent={onOpenAgent}
                    />
                  ))}
                </div>
              </LaneLabels.Provider>
              <ActivityFeed
                activity={session.activity}
                names={names}
                liveAgents={liveAgents}
                filters={activeFilters}
                onOpenAgent={onOpenAgent}
                toolbar={<FeedFilters filters={activeFilters} onChange={setFilters} agentOptions={agentOptions} toolOptions={toolOptions} stale={pruned > 0} />}
              />
              <Dock dock={dock} onChange={setDock} follow={tl.follow} onFollow={toggleFollow} onZoom={zoom} onFit={fit}>
                <Timeline
                  session={session}
                  names={names}
                  filters={activeFilters}
                  state={tl}
                  dispatch={tlDispatch}
                  fitPending={fitPending}
                  onFitDone={onFitDone}
                  infoRef={tlInfo}
                  reveal={reveal}
                  onOpenCall={onOpenCall}
                  onOpenAgent={onOpenAgent}
                />
              </Dock>
            </div>
          ) : (
            <EmptyState />
          )}
        </main>
      </div>
      {modalShown && session && modalAgent !== null ? (
        <div ref={modalWrap}>
          <AgentModal
            session={session}
            agentId={modalAgent}
            names={names}
            history={state.history.get(historyKey(session.id, modalAgent)) ?? NO_HISTORY}
            suspended={drawerShown || paletteShown || helpShown}
            returnTo={trigger.current}
            skipReturn={modalSkip}
            onClose={onCloseModal}
            onShowLane={onShowLane}
            onOpenAgent={onOpenAgent}
            onOpenCall={onOpenCall}
          />
        </div>
      ) : null}
      {drawerShown && open ? (
        <div ref={drawerWrap}>
          <CallDrawer
            key={`${open.sessionId}/${open.callId}`}
            sessionId={open.sessionId}
            callId={open.callId}
            tool={open.tool}
            status={openStatus}
            suspended={paletteShown || helpShown}
            returnTo={callTrigger.current}
            onClose={onClose}
          />
        </div>
      ) : null}
      {paletteShown && paletteCtx ? <Palette ctx={paletteCtx} skipReturn={paletteSkip} onClose={closePalette} onRun={runPaletteItem} /> : null}
      {helpShown ? <ShortcutsSheet onClose={closeHelp} returnTo={helpReturn.current} /> : null}
    </>
  );
}
