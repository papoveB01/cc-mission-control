import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import { ActivityFeed } from "./ActivityFeed";
import { AgentLane, LaneLabels } from "./AgentLane";
import { AgentModal } from "./AgentModal";
import { CallDrawer } from "./CallDrawer";
import { ContextPanel } from "./ContextPanel";
import { Dock } from "./Dock";
import { EmptyState } from "./EmptyState";
import { FeedFilters } from "./FeedFilters";
import { Palette } from "./Palette";
import { Panel } from "./Panel";
import { ShortcutsSheet } from "./ShortcutsSheet";
import { Telemetry } from "./Telemetry";
import { Timeline, type Reveal, type TimelineInfo } from "./Timeline";
import { Topology } from "./Topology";
import { defaultDock, loadDock, saveDock, type DockState } from "../dock";
import { loadFeedView, loadFilters, noFilters, saveFeedView, type FeedView, pruneFilters, saveFilters, safeStorage, type Filters } from "../filters";
import { scrollBehavior } from "../motion";
import type { PaletteContext, PaletteItem } from "../palette";
import { dispatchKey, isTypingTarget, type ShortcutAction } from "../shortcuts";
import { DOC_TITLE_NAME_MAX, historyKey, laneNames, sessionName, truncateName, liveAgentIds } from "../store";
import type { HistoryPoint } from "../store";
import { errorCalls, historyTimeline, initialTimeline, nextError, timelineReducer } from "../timeline";
import type { Session, ToolCall } from "../types";

const NO_HISTORY: readonly HistoryPoint[] = [];
const NO_LIVE: ReadonlySet<string> = new Set();
const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

interface OpenCall {
  sessionId: string;
  callId: string;
  tool: string;
}

export interface HudApi {
  /** Open a call in the detail drawer (also from outside the HUD body, e.g. a files table). */
  openCall: (callId: string, tool: string, trigger?: Element) => void;
}

export interface HudViewProps {
  /** The session to show, or null for the empty state. */
  session: Session | null;
  /** True for the live dashboard. False renders a finished, read-only replay. */
  live: boolean;
  /** URL of the full redacted call detail (drawer). */
  callDetailUrl: (sessionId: string, callId: string) => string;
  /** All sessions in tab order (palette and `[` `]` navigation). Defaults to just `session`. */
  sessions?: readonly Session[];
  onSelectSession?: (id: string) => void;
  /** Context-usage samples per agent (sparklines). Empty in history. */
  history?: ReadonlyMap<string, readonly HistoryPoint[]>;
  /** Bumped when the topology should re-measure (a fresh snapshot). */
  epoch?: number;
  /** The page header. Rendered inside the inert root, so overlays disable it. */
  renderHeader: (actions: ReactNode) => ReactNode;
  /** When it returns a node, that replaces the HUD body (Files, Usage tabs). Overlays stay available. */
  renderTab?: (api: HudApi) => ReactNode | null;
  /** id of the element labelling the main tabpanel. Defaults to the live session tab (`tab-<id>`). */
  tabLabelId?: string;
  /** A call-detail request got 401 (history: expired cookie). */
  onUnauthorized?: () => void;
  /** Text after the session name in the browser tab title. */
  titleSuffix?: string;
}

const NO_SESSIONS: readonly Session[] = [];
const NO_HISTORY_MAP: ReadonlyMap<string, readonly HistoryPoint[]> = new Map();

/** A replayed session is finished by definition: nothing in it can still be running or live. */
export function freezeSession(s: Session): Session {
  return s.status === "ended" && s.ended !== null ? s : { ...s, status: "ended", ended: s.ended ?? s.last_event };
}

export function HudView({ session: input, live, callDetailUrl, sessions: sessionsProp, onSelectSession, history = NO_HISTORY_MAP, epoch = 0, renderHeader, renderTab, tabLabelId, onUnauthorized, titleSuffix = "Mission Control" }: HudViewProps) {
  const [open, setOpen] = useState<OpenCall | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [modalAgent, setModalAgent] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [filters, setFilters] = useState<Filters>(() => (live ? loadFilters() : noFilters));
  const [dock, setDock] = useState<DockState>(() => loadDock(safeStorage()) ?? defaultDock);
  const [tl, tlDispatch] = useReducer(timelineReducer, live ? initialTimeline : historyTimeline);
  // A finished session opens with the whole timeline in view.
  const [fitPending, setFitPending] = useState(!live);
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

  useEffect(() => {
    if (live) saveFilters(filters);
  }, [filters, live]);
  useEffect(() => saveDock(dock, safeStorage()), [dock]);

  // A replay is frozen as ended (see freezeSession): no running pulses, no ticking timers.
  const session = useMemo(() => (live || !input ? input : freezeSession(input)), [input, live]);
  const selectedId = session?.id ?? null;
  const sessions = sessionsProp ?? (session ? [session] : NO_SESSIONS);

  const agents = session?.agents ?? [];
  const namesKey = agents.map((a) => `${a.id}\u0000${a.label}\u0000${a.started}`).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- names depend only on the fields in namesKey
  const names = useMemo(() => laneNames(agents), [namesKey]);

  const liveKey = agents.filter((a) => a.status === "running" || a.status === "waiting").map((a) => a.id).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- depends only on liveKey
  const liveAgents = useMemo(() => (live ? liveAgentIds(agents) : NO_LIVE), [liveKey, live]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const callsById = useMemo(() => {
    const m = new Map<string, ToolCall>();
    for (const a of agents) for (const c of a.calls) m.set(c.id, c);
    return m;
  }, [session?.agents]); // eslint-disable-line react-hooks/exhaustive-deps

  const [feedView, setFeedView] = useState<FeedView>(() => loadFeedView());
  const changeFeedView = useCallback((v: FeedView) => {
    setFeedView(v);
    saveFeedView(v);
  }, []);

  const toolKey = [...new Set(agents.flatMap((a) => Object.keys(a.tool_counts)))].sort().join("\u0001");
  const toolOptions = useMemo(() => (toolKey ? toolKey.split("\u0001") : []), [toolKey]);
  const agentOptions = useMemo(() => agents.map((a) => ({ id: a.id, label: names.get(a.id) ?? a.label })), [names, namesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Saved filters may name agents or tools that this session lacks: ignore those, and say so.
  const { filters: activeFilters, pruned } = useMemo(
    () => pruneFilters(filters, new Set(agentOptions.map((a) => a.id)), new Set(toolOptions)),
    [filters, agentOptions, toolOptions],
  );
  const matchFilters = useMemo<Filters>(() => ({ ...activeFilters, view: feedView }), [activeFilters, feedView]);

  // The browser tab shows the selected session's name.
  const docName = session ? sessionName(session) : null;
  useEffect(() => {
    document.title = docName ? `${truncateName(docName, DOC_TITLE_NAME_MAX)} \u00b7 ${titleSuffix}` : "Mission Control";
  }, [docName, titleSuffix]);

  // Overlays that are actually rendered drive inertness, scroll lock and Esc layering.
  const modalShown = modalAgent !== null && session !== null;
  const drawerShown = open !== null && session !== null && session.id === open.sessionId;
  const paletteShown = paletteOpen;
  const helpShown = helpOpen;
  const anyOverlay = modalShown || drawerShown || paletteShown || helpShown;

  // Leaving a session (tab switch, session gone) clears overlays tied to it.
  // Not on mount: the initial state is already fresh, and a reset there would undo the opening fit
  // of a replay (the fit effect in Timeline runs first, then this one).
  const shownId = useRef(selectedId);
  useEffect(() => {
    if (shownId.current === selectedId) return;
    shownId.current = selectedId;
    setOpen(null);
    setModalAgent(null);
    modalOpen.current = false;
    drawerOpen.current = false;
    // Timeline window and the error cursor belong to one session: start fresh (follow live, fit).
    tlDispatch({ type: "reset" });
    lastError.current = null;
    revealFocus.current = null;
    setReveal(null);
    setFitPending(!live);
  }, [selectedId]); // eslint-disable-line react-hooks/exhaustive-deps -- `live` is fixed for a mounted view

  const onSelect = useCallback((id: string) => {
    onSelectSession?.(id);
    setOpen(null);
    setModalAgent(null);
    modalOpen.current = false;
    drawerOpen.current = false;
  }, [onSelectSession]);

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
  /** Open a call by id (for views that list calls outside the HUD body). */
  const openCallById = useCallback(
    (callId: string, tool: string, el?: Element) => {
      if (!sid) return;
      if (!drawerOpen.current) callTrigger.current = el ?? document.activeElement;
      revealFocus.current = null;
      drawerOpen.current = true;
      setOpen({ sessionId: sid, callId, tool });
    },
    [sid],
  );
  const hudApi = useMemo<HudApi>(() => ({ openCall: openCallById }), [openCallById]);
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
      const idx = sessions.findIndex((s) => s.id === selectedId);
      switch (act) {
        case "help":
          helpReturn.current = document.activeElement;
          setHelpOpen(true);
          break;
        case "prev-session":
        case "next-session": {
          const n = sessions.length;
          const t = sessions[(idx + (act === "next-session" ? 1 : -1) + n) % n];
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
    [sessions, selectedId, onSelect, toggleDock, jumpNextError],
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
            if (!live) break;
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
    [onSelect, onOpenAgent, onOpenCall, toggleDock, toggleFollow, fit, jumpNextError, live],
  );
  const closeHelp = useCallback(() => setHelpOpen(false), []);

  // The palette works on a snapshot taken when it opens, refreshed at most once a second, so
  // results do not jump under the cursor while updates arrive every 150 ms.
  const paletteSource = useRef({ sessions, session, selectedId, names });
  paletteSource.current = { sessions, session, selectedId, names };
  useEffect(() => {
    if (!paletteOpen) {
      setPaletteCtx(null);
      return;
    }
    const take = (): void => {
      const src = paletteSource.current;
      // The tab list may hold older copies of the selected session: use the current one for its calls.
      const list = src.session ? src.sessions.map((s) => (s.id === src.selectedId ? (src.session as Session) : s)) : src.sessions;
      setPaletteCtx({ sessions: list, selectedId: src.selectedId, names: src.names, live });
    };
    take();
    const t = setInterval(take, 1000);
    return () => clearInterval(t);
  }, [paletteOpen, live]);

  const tabContent = session && renderTab ? renderTab(hudApi) : null;

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
      <div ref={appRoot} className={live ? undefined : "hud-static"}>
        {renderHeader(headerActions)}
        {session ? <Telemetry session={session} /> : null}
        <main
          id="session-panel"
          tabIndex={-1}
          role={session ? "tabpanel" : undefined}
          aria-labelledby={session ? (tabLabelId ?? `tab-${session.id}`) : undefined}
          className="main"
        >
          {session && tabContent !== null ? (
            tabContent
          ) : session ? (
            <div className="body">
              <div className="col-side">
                <Panel title="Topology" id="topology-panel">
                  <Topology key={session.id} agents={session.agents} names={names} selected={modalShown ? modalAgent : null} epoch={epoch} onOpenAgent={onOpenAgent} />
                </Panel>
                <Panel title="Context" id="context-panel">
                  <ContextPanel sessionId={session.id} agents={session.agents} names={names} history={history} onOpenAgent={onOpenAgent} />
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
                      filters={matchFilters}
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
                calls={callsById}
                view={feedView}
                onViewChange={changeFeedView}
                filters={matchFilters}
                onOpenAgent={onOpenAgent}
                toolbar={<FeedFilters filters={activeFilters} onChange={setFilters} agentOptions={agentOptions} toolOptions={toolOptions} stale={pruned > 0} />}
              />
              <Dock dock={dock} onChange={setDock} live={live} follow={tl.follow} onFollow={toggleFollow} onZoom={zoom} onFit={fit}>
                <Timeline
                  session={session}
                  names={names}
                  filters={matchFilters}
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
            history={history.get(historyKey(session.id, modalAgent)) ?? NO_HISTORY}
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
            urlFor={callDetailUrl}
            onUnauthorized={onUnauthorized}
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
