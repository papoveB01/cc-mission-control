import { formatClock, formatClockSeconds } from "./format";
import type { Agent, Session, ToolCall } from "./types";

export const MIN_SPAN = 2;
export const MAX_SPAN = 86_400;
export const DEFAULT_SPAN = 120;
/** Fraction of the plot where "now" sits while following live. */
export const FOLLOW_ANCHOR = 0.98;
export const MIN_BAR_PX = 3;

export interface View {
  start: number;
  span: number;
}

export interface Bounds {
  t0: number;
  t1: number;
}

export interface TimelineState {
  /** Left edge (epoch seconds) while not following. */
  start: number;
  /** Visible seconds. While `auto`, this is only the upper limit of the automatic window. */
  span: number;
  follow: boolean;
  /** Automatic zoom: show the whole session until it is longer than `span`, then slide. */
  auto: boolean;
}

export type TimelineAction =
  | { type: "pan"; dt: number; view: View; bounds?: Bounds }
  | { type: "zoom"; factor: number; anchor: number; view: View; bounds?: Bounds }
  | { type: "fit"; t0: number; t1: number; live: boolean }
  | { type: "center"; t: number; view: View; bounds?: Bounds }
  | { type: "follow"; on: boolean; view?: View }
  | { type: "reset" };

export const initialTimeline: TimelineState = { start: 0, span: DEFAULT_SPAN, follow: true, auto: true };

const clampSpan = (span: number): number => Math.min(MAX_SPAN, Math.max(MIN_SPAN, span));

/** Keep the window within [t0 - span/2, edge + span/2] so it cannot be panned into the void. */
export function clampStart(start: number, span: number, bounds?: Bounds): number {
  if (!bounds) return start;
  return Math.min(bounds.t1 + span * 0.5, Math.max(bounds.t0 - span * 0.5, start));
}

/**
 * The window shown while following live. Derived from the clock, so no state update is needed
 * each second. Automatic zoom shows the whole session until it outgrows `span`.
 */
export function followView(s: Pick<TimelineState, "span" | "auto">, t0: number, edge: number): View {
  const len = Math.max(0, edge - t0);
  if (s.auto && len * 1.08 <= s.span) {
    return { start: t0 - len * 0.04, span: clampSpan(len * 1.08) };
  }
  return { start: edge - s.span * FOLLOW_ANCHOR, span: s.span };
}

export function timelineReducer(s: TimelineState, a: TimelineAction): TimelineState {
  switch (a.type) {
    case "pan": {
      const span = a.view.span;
      return { start: clampStart(a.view.start + a.dt, span, a.bounds), span, follow: false, auto: false };
    }
    case "zoom": {
      // While following, the right edge (now) stays put; the window is derived from the span.
      const span = clampSpan(a.view.span / a.factor);
      if (s.follow) return { ...s, span, auto: false };
      const anchor = Math.min(1, Math.max(0, a.anchor));
      const at = a.view.start + anchor * a.view.span;
      return { ...s, span, auto: false, start: clampStart(at - anchor * span, span, a.bounds) };
    }
    case "fit": {
      const len = Math.max(a.t1 - a.t0, 0);
      const span = clampSpan(len * 1.06 || DEFAULT_SPAN);
      // A fit that includes "now" keeps following; a fit of a finished session does not.
      return { start: a.t0 - (span - len) / 2, span, follow: a.live, auto: false };
    }
    case "center": {
      const span = a.view.span;
      return { start: clampStart(a.t - span / 2, span, a.bounds), span, follow: false, auto: false };
    }
    case "follow":
      if (a.on) return { ...s, follow: true };
      return a.view ? { start: a.view.start, span: a.view.span, follow: false, auto: false } : { ...s, follow: false };
    case "reset":
      return initialTimeline;
  }
}

export const xOf = (t: number, s: View, width: number): number => ((t - s.start) / s.span) * width;
export const tOf = (x: number, s: View, width: number): number => s.start + (x / width) * s.span;

export interface BarLayout {
  x: number;
  w: number;
  running: boolean;
  /** Bar overlaps the visible plot. */
  visible: boolean;
}

/** Bar geometry. A running call extends to `now`; ended calls use `ended` (or the duration). */
export function barLayout(call: Pick<ToolCall, "started" | "ended" | "duration_ms" | "status">, s: View, width: number, now: number): BarLayout {
  const running = call.status === "running";
  const end = running ? Math.max(now, call.started) : (call.ended ?? call.started + (call.duration_ms ?? 0) / 1000);
  const x = xOf(call.started, s, width);
  const w = Math.max(MIN_BAR_PX, xOf(end, s, width) - x);
  return { x, w, running, visible: x + w >= 0 && x <= width };
}

/** Whether a bar is wide enough to carry its tool name (about 6.5 px per character plus padding). */
export const fitsLabel = (w: number, label: string): boolean => w >= label.length * 6.5 + 10;

const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400];

export interface Tick {
  t: number;
  label: string;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const defaultOffset = (t: number): number => -new Date(t * 1000).getTimezoneOffset() * 60;

function tickLabel(t: number, step: number): string {
  const d = new Date(t * 1000);
  const date = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  if (step >= 86_400) return date;
  if (step >= 43_200) return `${date} ${formatClock(t)}`;
  return step < 60 ? formatClockSeconds(t) : formatClock(t);
}

/**
 * Axis ticks at "nice" local-time multiples, at least `minPx` apart. The UTC offset is looked
 * up per tick (`offsetAt`), so ticks stay aligned across a DST change.
 */
export function niceTicks(s: View, width: number, minPx = 96, offsetAt: (t: number) => number = defaultOffset): Tick[] {
  if (width <= 0 || s.span <= 0) return [];
  const want = (s.span * minPx) / width;
  const step = STEPS.find((x) => x >= want) ?? (STEPS[STEPS.length - 1] as number);
  const out: Tick[] = [];
  let k = Math.ceil((s.start + offsetAt(s.start)) / step);
  for (let guard = 0; guard < 400; guard++, k++) {
    const local = k * step;
    const t = local - offsetAt(local - offsetAt(s.start));
    if (t > s.start + s.span) break;
    if (t >= s.start) out.push({ t, label: tickLabel(t, step) });
  }
  return out;
}

export interface Extent {
  t0: number;
  t1: number;
  running: boolean;
}

/** Time range covered by the session's calls. `now` caps running calls. */
export function sessionExtent(session: Pick<Session, "agents" | "status" | "ended" | "last_event" | "started">, now: number): Extent {
  let t0 = Infinity;
  let t1 = -Infinity;
  let running = false;
  for (const a of session.agents) {
    for (const c of a.calls) {
      if (c.started < t0) t0 = c.started;
      const end = c.status === "running" ? now : (c.ended ?? c.started);
      if (c.status === "running") running = true;
      if (end > t1) t1 = end;
    }
  }
  if (!Number.isFinite(t0)) return { t0: session.started, t1: Math.max(session.started + 1, session.last_event), running: false };
  if (session.status === "ended") running = false;
  return { t0, t1, running };
}

/** The "now" edge the follow-live view tracks: the clock while something runs, else the last activity. */
export function followEdge(extent: Extent, now: number): number {
  return extent.running ? now : extent.t1;
}

export interface RowBar {
  call: ToolCall;
  agentId: string;
}

/** Calls of one agent in start order (for left/right keyboard navigation). */
const barCache = new WeakMap<object, ToolCall[]>();
export function rowBars(agent: Pick<Agent, "calls">): ToolCall[] {
  // Keyed by the calls array: structural sharing keeps it stable between unrelated updates.
  let list = barCache.get(agent.calls);
  if (!list) {
    list = [...agent.calls].sort((a, b) => a.started - b.started);
    barCache.set(agent.calls, list);
  }
  return list;
}

export const TEXT_ALT_LIMIT = 200;

export interface AltRow {
  agentId: string;
  call: ToolCall;
}

/** Newest calls across all agents for the text alternative, capped. `total` is the uncapped count. */
export function altRows(agents: readonly Pick<Agent, "id" | "calls">[], limit = TEXT_ALT_LIMIT): { rows: AltRow[]; total: number } {
  const all = agents.flatMap((a) => a.calls.map((call) => ({ agentId: a.id, call })));
  all.sort((a, b) => b.call.started - a.call.started);
  return { rows: all.slice(0, limit), total: all.length };
}

/** Nearest bar in `bars` by start time (for up/down navigation between rows). */
export function nearestBar(bars: readonly ToolCall[], t: number): ToolCall | null {
  let best: ToolCall | null = null;
  let d = Infinity;
  for (const b of bars) {
    const dd = Math.abs(b.started - t);
    if (dd < d) {
      d = dd;
      best = b;
    }
  }
  return best;
}

/** Errors across the session, oldest first, for the "next error" shortcut. */
export function errorCalls(session: Pick<Session, "agents">): ToolCall[] {
  const order = new Map(session.agents.map((a, i) => [a.id, i]));
  return session.agents
    .flatMap((a) => a.calls.filter((c) => c.status === "error"))
    .sort((a, b) => a.started - b.started || (order.get(a.agent_id) ?? 0) - (order.get(b.agent_id) ?? 0) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

/** Next error after `currentId` (wrapping); the first error when `currentId` is null or gone. */
export function nextError(errors: readonly ToolCall[], currentId: string | null): ToolCall | null {
  if (errors.length === 0) return null;
  const i = errors.findIndex((c) => c.id === currentId);
  return errors[(i + 1) % errors.length] ?? null;
}
