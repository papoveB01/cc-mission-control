import { memo, useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type KeyboardEvent as ReactKeyboardEvent, type MutableRefObject } from "react";
import { useNowWhen } from "../clock";
import { matchCall, type Filters, filtersActive } from "../filters";
import { formatClockSeconds, formatDuration } from "../format";
import {
  altRows, barLayout, fitsLabel, followEdge, followView, nearestBar, niceTicks, rowBars, sessionExtent, xOf,
  type Bounds, type TimelineAction, type TimelineState, type View,
} from "../timeline";
import type { Agent, Session, ToolCall } from "../types";
import { MiddleText } from "./MiddleText";

const LABEL_W = 132;
const ROW_H = 24;
const BAR_H = 14;
const AXIS_H = 22;

/** What the dock controls and the palette need to know about the current window. */
export interface TimelineInfo {
  view: View;
  bounds: Bounds;
  edge: number;
  /** The session still has running calls (a fit then keeps following). */
  live: boolean;
  t0: number;
  t1: number;
}

export interface Reveal {
  callId: string;
  agentId: string;
  started: number;
  nonce: number;
}

interface BarProps {
  call: ToolCall;
  x: number;
  w: number;
  dim: boolean;
  running: boolean;
  tabbable: boolean;
  label: boolean;
  onOpen: (call: ToolCall, el: Element) => void;
  onKey: (e: ReactKeyboardEvent<SVGGElement>, call: ToolCall) => void;
  onTip: (call: ToolCall | null, el: Element | null) => void;
  onFocusBar: (call: ToolCall) => void;
}

const Bar = memo(function Bar({ call, x, w, dim, running, tabbable, label, onOpen, onKey, onTip, onFocusBar }: BarProps) {
  const status = call.status === "ok" ? "succeeded" : call.status === "error" ? "failed" : "running";
  return (
    <g
      className={`tl-bar st-${call.status}${dim ? " dim" : ""}`}
      transform={`translate(${x.toFixed(1)} ${(ROW_H - BAR_H) / 2})`}
      role="button"
      tabIndex={tabbable ? 0 : -1}
      aria-label={`${call.tool}, ${status}. ${call.summary}. Open call.`}
      data-call-id={call.id}
      data-agent-id={call.agent_id}
      onClick={(e) => onOpen(call, e.currentTarget)}
      onKeyDown={(e) => onKey(e, call)}
      onMouseEnter={(e) => onTip(call, e.currentTarget)}
      onMouseLeave={() => onTip(null, null)}
      onFocus={(e) => {
        onFocusBar(call);
        onTip(call, e.currentTarget);
      }}
      onBlur={() => onTip(null, null)}
    >
      <rect className="tl-rect" width={w} height={BAR_H} rx="2" />
      {label ? (
        <text className="tl-text" x="5" y={BAR_H / 2} dy="0.35em">
          {call.tool}
        </text>
      ) : null}
      {running ? <rect className="tl-edge" x={Math.max(0, w - 2)} width="2" height={BAR_H} /> : null}
    </g>
  );
});

interface RowProps {
  agent: { id: string; name: string; sub: boolean };
  list: ToolCall[];
  start: number;
  span: number;
  width: number;
  /** Clock for running bars; 0 for rows with nothing running so idle rows do not re-render. */
  now: number;
  ended: boolean;
  tabId: string | undefined;
  ticks: { t: number }[];
  filters: Filters;
  onOpen: (call: ToolCall, el: Element) => void;
  onKey: (e: ReactKeyboardEvent<SVGGElement>, call: ToolCall) => void;
  onTip: (call: ToolCall | null, el: Element | null) => void;
  onFocusBar: (call: ToolCall) => void;
  onOpenAgent: (id: string, el: Element) => void;
}

const Row = memo(function Row({ agent, list, start, span, width, now, ended, tabId, ticks, filters, onOpen, onKey, onTip, onFocusBar, onOpenAgent }: RowProps) {
  const view = { start, span };
  const active = filtersActive(filters);
  return (
    <div className="tl-row" style={{ height: ROW_H }} data-agent-row={agent.id}>
      <button type="button" className={`tl-label${agent.sub ? " sub" : ""}`} style={{ width: LABEL_W }} title={`${agent.name}: open agent details`} onClick={(e) => onOpenAgent(agent.id, e.currentTarget)}>
        <MiddleText text={agent.name} />
      </button>
      <svg className="tl-plot" width={width} height={ROW_H} role="group" aria-label={`${agent.name} calls`}>
        {ticks.map((t) => (
          <line key={t.t} className="tl-grid" x1={xOf(t.t, view, width)} x2={xOf(t.t, view, width)} y2={ROW_H} />
        ))}
        {list.map((c) => {
          const l = barLayout(c, view, width, now || Date.now() / 1000);
          if (!l.visible && c.id !== tabId) return null;
          return (
            <Bar
              key={c.id}
              call={c}
              x={l.x}
              w={l.w}
              dim={active && !matchCall(filters, c)}
              running={l.running && !ended}
              tabbable={c.id === tabId}
              label={fitsLabel(l.w, c.tool)}
              onOpen={onOpen}
              onKey={onKey}
              onTip={onTip}
              onFocusBar={onFocusBar}
            />
          );
        })}
      </svg>
    </div>
  );
});

const CallTable = memo(function CallTable({ agents, names }: { agents: Agent[]; names: ReadonlyMap<string, string> }) {
  const { rows, total } = useMemo(() => altRows(agents), [agents]);
  const nameOf = (id: string): string => (id === "main" ? "Main" : (names.get(id) ?? id));
  return (
    // The wrapper must be a div: `overflow` and `clip-path` do not clip a table box, so a table with
    // long nowrap summaries would otherwise stretch the dock sideways (and push the labels off-screen).
    <div className="sr-only">
    <table>
      <caption>
        Tool calls timeline, showing the newest {rows.length} of {total} calls
      </caption>
      <thead>
        <tr><th>Agent</th><th>Tool</th><th>Status</th><th>Started</th><th>Duration</th><th>Summary</th></tr>
      </thead>
      <tbody>
        {rows.map(({ agentId, call: c }) => (
          <tr key={c.id}>
            <td>{nameOf(agentId)}</td><td>{c.tool}</td><td>{c.status}</td><td>{formatClockSeconds(c.started)}</td>
            <td>{c.duration_ms !== null ? formatDuration(c.duration_ms) : "running"}</td><td>{c.summary}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
});

interface Tip {
  call: ToolCall;
  agent: string;
  x: number;
  y: number;
}

/** Normalise wheel deltas to pixels (lines and pages are common with mice and some browsers). */
function wheelPixels(e: WheelEvent, pageH: number): { dx: number; dy: number } {
  const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? pageH : 1;
  return { dx: e.deltaX * k, dy: e.deltaY * k };
}

interface Props {
  session: Session;
  names: ReadonlyMap<string, string>;
  filters: Filters;
  state: TimelineState;
  dispatch: Dispatch<TimelineAction>;
  /** A fit was requested (button or palette) and has not been applied yet. */
  fitPending: boolean;
  onFitDone: () => void;
  infoRef: MutableRefObject<TimelineInfo | null>;
  reveal: Reveal | null;
  onOpenCall: (call: ToolCall, trigger?: Element) => void;
  onOpenAgent: (agentId: string, trigger?: Element) => void;
}

export const Timeline = memo(function Timeline({ session, names, filters, state, dispatch, fitPending, onFitDone, infoRef, reveal, onOpenCall, onOpenAgent }: Props) {
  const rowsRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [tip, setTip] = useState<Tip | null>(null);
  const [rowTab, setRowTab] = useState<Record<string, string>>({});
  const dragged = useRef(false);
  const focusRow = useRef<string | null>(null);

  // Subscribe to the 1 s clock only while a call is running in a live session.
  const running = useMemo(() => session.status === "active" && session.agents.some((a) => a.calls.some((c) => c.status === "running")), [session]);
  const nowMs = useNowWhen(running);
  const now = running ? nowMs / 1000 : session.ended ?? session.last_event;
  const extent = useMemo(() => sessionExtent(session, now), [session, now]);
  const edge = followEdge(extent, now);
  // The follow-live window is derived from the clock here; nothing is dispatched per second.
  const view: View = state.follow ? followView(state, extent.t0, edge) : { start: state.start, span: state.span };
  const bounds: Bounds = { t0: extent.t0, t1: edge };
  infoRef.current = { view, bounds, edge, live: extent.running, t0: extent.t0, t1: extent.t1 };

  useEffect(() => {
    if (fitPending) {
      dispatch({ type: "fit", t0: extent.t0, t1: extent.t1, live: extent.running });
      onFitDone();
    }
  }, [fitPending, extent, dispatch, onFitDone]);

  useEffect(() => {
    if (!reveal) return;
    dispatch({ type: "center", t: reveal.started, view: infoRef.current?.view ?? view, bounds });
    setRowTab((m) => ({ ...m, [reveal.agentId]: reveal.callId }));
  }, [reveal?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = rowsRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(200, el.clientWidth - LABEL_W)));
    ro.observe(el);
    setWidth(Math.max(200, el.clientWidth - LABEL_W));
    return () => ro.disconnect();
  }, []);

  // On narrow screens the dock scrolls sideways; show the live (right) end first.
  useEffect(() => {
    const host = rowsRef.current?.closest<HTMLElement>(".dock-body");
    if (host && host.scrollWidth > host.clientWidth) host.scrollLeft = host.scrollWidth;
  }, [width]);

  const ticks = useMemo(() => niceTicks({ start: view.start, span: view.span }, width), [view.start, view.span, width]);

  // Ctrl/Cmd + wheel (and trackpad pinch) zooms; horizontal wheel or Shift + wheel pans.
  // A plain vertical wheel is left alone so rows and the page scroll normally.
  const live = useRef({ width, dispatch });
  live.current = { width, dispatch };
  useEffect(() => {
    const el = rowsRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent): void => {
      const info = infoRef.current;
      if (!info) return;
      const { dx, dy } = wheelPixels(e, el.clientHeight);
      const w = live.current.width;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const rate = Math.abs(dy) < 50 ? 0.01 : 0.0025;
        const factor = Math.min(1.5, Math.max(1 / 1.5, Math.exp(-dy * rate)));
        const rect = el.getBoundingClientRect();
        const anchor = Math.min(1, Math.max(0, (e.clientX - rect.left - LABEL_W) / w));
        live.current.dispatch({ type: "zoom", factor, anchor, view: info.view, bounds: info.bounds });
      } else if (e.shiftKey || Math.abs(dx) > Math.abs(dy)) {
        e.preventDefault();
        live.current.dispatch({ type: "pan", dt: ((dx || dy) / w) * info.view.span, view: info.view, bounds: info.bounds });
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [infoRef]);

  const drag = useRef<{ x: number; id: number } | null>(null);
  const onPointerDown = (e: React.PointerEvent): void => {
    if (e.button !== 0 || (e.target as Element).closest("button")) return;
    drag.current = { x: e.clientX, id: e.pointerId };
    dragged.current = false;
  };
  const onPointerMove = (e: React.PointerEvent): void => {
    const d = drag.current;
    const info = infoRef.current;
    if (!d || !info) return;
    const dx = e.clientX - d.x;
    if (!dragged.current && Math.abs(dx) < 4) return;
    if (!dragged.current) rowsRef.current?.setPointerCapture(d.id);
    dragged.current = true;
    d.x = e.clientX;
    dispatch({ type: "pan", dt: (-dx / width) * info.view.span, view: info.view, bounds: info.bounds });
  };
  const endDrag = (e: React.PointerEvent): void => {
    if (drag.current && rowsRef.current?.hasPointerCapture(e.pointerId)) rowsRef.current.releasePointerCapture(e.pointerId);
    drag.current = null;
    setTimeout(() => {
      dragged.current = false;
    }, 0);
  };

  const open = useCallback(
    (call: ToolCall, el: Element) => {
      if (!dragged.current) onOpenCall(call, el);
    },
    [onOpenCall],
  );

  const agents = session.agents;
  const bars = useMemo(() => new Map(agents.map((a) => [a.id, rowBars(a)])), [agents]);

  const focusBar = useCallback(
    (agentId: string, call: ToolCall) => {
      setRowTab((m) => ({ ...m, [agentId]: call.id }));
      const info = infoRef.current;
      if (info) {
        const x = xOf(call.started, info.view, width);
        if (x < 0 || x > width - 12) dispatch({ type: "center", t: call.started, view: info.view, bounds: info.bounds });
      }
      requestAnimationFrame(() => {
        rowsRef.current?.querySelector<SVGGElement>(`[data-call-id="${CSS.escape(call.id)}"]`)?.focus();
      });
    },
    [dispatch, width, infoRef],
  );

  const onKey = useCallback(
    (e: ReactKeyboardEvent<SVGGElement>, call: ToolCall) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpenCall(call, e.currentTarget);
        return;
      }
      const list = bars.get(call.agent_id) ?? [];
      const i = list.findIndex((c) => c.id === call.id);
      let target: ToolCall | undefined;
      let agentId = call.agent_id;
      if (e.key === "ArrowRight") target = list[i + 1];
      else if (e.key === "ArrowLeft") target = list[i - 1];
      else if (e.key === "Home") target = list[0];
      else if (e.key === "End") target = list[list.length - 1];
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const dir = e.key === "ArrowDown" ? 1 : -1;
        let r = agents.findIndex((a) => a.id === call.agent_id) + dir;
        while (r >= 0 && r < agents.length) {
          const row = agents[r];
          const nb = row ? nearestBar(bars.get(row.id) ?? [], call.started) : null;
          if (row && nb) {
            target = nb;
            agentId = row.id;
            break;
          }
          r += dir;
        }
      } else return;
      e.preventDefault();
      if (target) focusBar(agentId, target);
    },
    [agents, bars, focusBar, onOpenCall],
  );

  const onTip = useCallback(
    (call: ToolCall | null, el: Element | null) => {
      if (!call || !el || !rowsRef.current) return setTip(null);
      const host = rowsRef.current.getBoundingClientRect();
      const r = el.getBoundingClientRect();
      const a = agents.find((x) => x.id === call.agent_id);
      setTip({ call, agent: call.agent_id === "main" ? "Main" : (names.get(call.agent_id) ?? a?.label ?? call.agent_id), x: r.left - host.left + r.width / 2, y: r.top - host.top });
    },
    [agents, names],
  );
  const onFocusBar = useCallback((call: ToolCall) => {
    focusRow.current = call.agent_id;
    setRowTab((m) => (m[call.agent_id] === call.id ? m : { ...m, [call.agent_id]: call.id }));
  }, []);

  // A focused bar can disappear (its call rolled off). Keep focus in the plot on the row's tab stop.
  useEffect(() => {
    const row = focusRow.current;
    if (row && document.activeElement === document.body) {
      rowsRef.current?.querySelector<SVGGElement>(`[data-agent-row="${CSS.escape(row)}"] .tl-bar[tabindex="0"]`)?.focus();
    }
  });
  const onFocusOut = (e: React.FocusEvent): void => {
    const target = e.target as Element;
    requestAnimationFrame(() => {
      // A removed element does not count as the user leaving the plot.
      if (target.isConnected && !rowsRef.current?.contains(document.activeElement)) focusRow.current = null;
    });
  };

  const nameOf = (a: Agent): string => (a.id === "main" ? "Main" : (names.get(a.id) ?? a.label));
  const ended = session.status === "ended";

  return (
    <div className="tl">
      <div className="tl-axis" style={{ height: AXIS_H }} aria-hidden="true">
        <div className="tl-gutter" style={{ width: LABEL_W }} />
        <svg width={width} height={AXIS_H}>
          {ticks.map((t) => {
            const x = xOf(t.t, view, width);
            return (
              <g key={t.t} transform={`translate(${x.toFixed(1)} 0)`}>
                <line y1={AXIS_H - 6} y2={AXIS_H} className="tl-tick" />
                <text y={AXIS_H - 9} x="3" className="tl-tick-label">{t.label}</text>
              </g>
            );
          })}
        </svg>
      </div>
      <div
        className="tl-rows"
        ref={rowsRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onBlurCapture={onFocusOut}
      >
        {agents.map((a) => {
          const list = bars.get(a.id) ?? [];
          const tabId = rowTab[a.id] && list.some((c) => c.id === rowTab[a.id]) ? rowTab[a.id] : list[list.length - 1]?.id;
          const rowRunning = !ended && list.some((c) => c.status === "running");
          return (
            <Row
              key={a.id}
              agent={{ id: a.id, name: nameOf(a), sub: a.id !== "main" }}
              list={list}
              start={view.start}
              span={view.span}
              width={width}
              now={rowRunning || ended ? now : 0}
              ended={ended}
              tabId={tabId}
              ticks={ticks}
              filters={filters}
              onOpen={open}
              onKey={onKey}
              onTip={onTip}
              onFocusBar={onFocusBar}
              onOpenAgent={onOpenAgent}
            />
          );
        })}
        {tip ? (
          <div className="tooltip tl-tip" role="tooltip" style={{ left: Math.min(Math.max(tip.x, 100), LABEL_W + width - 100), top: tip.y }}>
            <strong>{tip.call.tool}</strong>
            <span className="tl-tip-summary">{tip.call.summary}</span>
            <span className="tnum">
              {tip.agent} · {tip.call.status === "running" ? "running" : formatDuration(tip.call.duration_ms ?? ((tip.call.ended ?? tip.call.started) - tip.call.started) * 1000)}
            </span>
          </div>
        ) : null}
        {agents.length === 0 ? <p className="muted tl-empty">No agents yet.</p> : null}
      </div>
      <CallTable agents={agents} names={names} />
    </div>
  );
});
