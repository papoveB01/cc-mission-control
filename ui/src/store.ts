import { formatClock, formatClockSeconds } from "./format";
import type { Activity, Agent, NameSource, Session, ToolCall } from "./types";

export type Sessions = ReadonlyMap<string, Session>;

export const HISTORY_CAP = 300;
/** A pending compaction flag expires after this many Main samples or this many milliseconds. */
export const PENDING_MAX_SAMPLES = 3;
export const PENDING_MAX_MS = 60_000;

export interface PendingCompaction {
  at: number;
  samples: number;
}
export type PendingMap = ReadonlyMap<string, PendingCompaction>;

/** One context sample for an agent. `drop` marks a compaction. */
export interface HistoryPoint {
  /** Client receive time, epoch milliseconds. */
  t: number;
  tokens: number;
  drop: boolean;
}

/** Keyed by `${sessionId}/${agentId}`. Arrays are replaced (never mutated) when they change. */
export type History = ReadonlyMap<string, readonly HistoryPoint[]>;

export const historyKey = (sessionId: string, agentId: string): string => `${sessionId}/${agentId}`;

export interface MissionState {
  sessions: Sessions;
  version: string | null;
  history: History;
  /** Sessions whose compaction counter rose and whose Main has not yet shown the drop. */
  pending: PendingMap;
  /** Bumped on every snapshot (connect or reconnect); views use it to reset baselines. */
  snapshots: number;
}

export type ServerMessage =
  | { type: "snapshot"; version: string; sessions: Session[] }
  | { type: "sessions"; sessions: Session[] };

export const initialState: MissionState = { sessions: new Map(), version: null, history: new Map(), pending: new Map(), snapshots: 0 };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isSession(v: unknown): v is Session {
  return (
    isRecord(v) &&
    typeof v.id === "string" &&
    typeof v.title === "string" &&
    typeof v.started === "number" &&
    Array.isArray(v.agents) &&
    Array.isArray(v.activity)
  );
}

const NAME_SOURCES: readonly string[] = ["custom", "generated", "folder"];

/**
 * Narrow the optional session-name fields at the JSON boundary: anything that is not a
 * non-empty string (or a known source) is dropped, so an older server simply has no `name`.
 */
export function narrowName(s: Session): Session {
  const raw = s as Session & { name?: unknown; name_source?: unknown };
  const { name, name_source, ...rest } = raw;
  const out: Session = { ...rest };
  if (typeof name === "string" && name.trim() !== "") out.name = name;
  if (typeof name_source === "string" && NAME_SOURCES.includes(name_source)) out.name_source = name_source as NameSource;
  return out;
}

/** Parse a raw WS frame. Returns null for anything that is not a known message. */
export function parseMessage(raw: unknown): ServerMessage | null {
  let data: unknown = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!isRecord(data) || !Array.isArray(data.sessions)) return null;
  const sessions = data.sessions.filter(isSession).map(narrowName);
  if (data.type === "snapshot") {
    return { type: "snapshot", version: typeof data.version === "string" ? data.version : "", sessions };
  }
  if (data.type === "sessions") return { type: "sessions", sessions };
  return null;
}

function shallowEqual(a: object, b: object): boolean {
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  const ra = a as Record<string, unknown>;
  const rb = b as Record<string, unknown>;
  return ka.every((k) => Object.is(ra[k], rb[k]));
}

/** Reuse previous items (by identity) where the new item is structurally equal. */
function shareList<T extends object>(
  prev: readonly T[],
  next: T[],
  keyOf: ((item: T) => string) | null,
  merge: (p: T, n: T) => T,
): T[] {
  let byKey: Map<string, T> | null = null;
  if (keyOf) {
    byKey = new Map();
    for (const p of prev) byKey.set(keyOf(p), p);
  }
  const out = next.map((n, i) => {
    const p = keyOf ? byKey?.get(keyOf(n)) : prev[i];
    return p ? merge(p, n) : n;
  });
  return out.length === prev.length && out.every((x, i) => x === prev[i]) ? (prev as T[]) : out;
}

const keepIfEqual = <T extends object>(p: T, n: T): T => (shallowEqual(p, n) ? p : n);

function shareAgent(p: Agent, n: Agent): Agent {
  const calls = shareList<ToolCall>(p.calls, n.calls, (c) => c.id, keepIfEqual);
  const tool_counts = shallowEqual(p.tool_counts, n.tool_counts) ? p.tool_counts : n.tool_counts;
  const candidate: Agent = { ...n, calls, tool_counts };
  return shallowEqual(candidate, p) ? p : candidate;
}

/** Stable activity keys: duplicates are counted from the newest end, so dropping old rows never renumbers. */
export function activityKeys(activity: readonly Activity[]): string[] {
  const seen = new Map<string, number>();
  const keys: string[] = new Array<string>(activity.length);
  for (let i = activity.length - 1; i >= 0; i--) {
    const e = activity[i] as Activity;
    const base = `${e.t}|${e.agent_id}|${e.kind}|${e.text}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    keys[i] = `${base}|${n}`;
  }
  return keys;
}

function shareActivity(prev: readonly Activity[], next: Activity[]): Activity[] {
  const prevKeys = activityKeys(prev);
  const byKey = new Map<string, Activity>();
  prev.forEach((a, i) => byKey.set(prevKeys[i] as string, a));
  const nextKeys = activityKeys(next);
  const out = next.map((n, i) => {
    const p = byKey.get(nextKeys[i] as string);
    return p && shallowEqual(p, n) ? p : n;
  });
  return out.length === prev.length && out.every((x, i) => x === prev[i]) ? (prev as Activity[]) : out;
}

export function shareSession(prev: Session | undefined, next: Session): Session {
  if (!prev) return next;
  const agents = shareList<Agent>(prev.agents, next.agents, (a) => a.id, shareAgent);
  const activity = shareActivity(prev.activity, next.activity);
  const candidate: Session = { ...next, agents, activity };
  return shallowEqual(candidate, prev) ? prev : candidate;
}

function without(map: PendingMap, id: string): PendingMap {
  if (!map.has(id)) return map;
  const next = new Map(map);
  next.delete(id);
  return next;
}

interface Recorded {
  history: History;
  pending: PendingMap;
}

/** Drop history keys of `sessionId` whose agent is not in `keep`. */
function pruneSession(history: History, sessionId: string, keep: ReadonlySet<string>): History {
  const prefix = `${sessionId}/`;
  let out: Map<string, readonly HistoryPoint[]> | null = null;
  for (const key of history.keys()) {
    if (key.startsWith(prefix) && !keep.has(key.slice(prefix.length))) {
      if (!out) out = new Map(history);
      out.delete(key);
    }
  }
  return out ?? history;
}

/**
 * Append context samples for one session. A sample is recorded when `context_tokens` changes.
 * A rising compaction counter sets a pending flag; the next Main sample whose tokens fall is
 * marked as the compaction drop. A falling counter (server restart) resets the session.
 * Histories of agents that left the session are pruned. Returns the same maps when nothing changed.
 */
export function recordHistory(history: History, pending: PendingMap, prev: Session | undefined, next: Session, now: number): Recorded {
  let pend = pending;
  let hist = history;
  if (prev && next.compactions < prev.compactions) {
    hist = pruneSession(hist, next.id, new Set());
    pend = without(pend, next.id);
  } else if (prev && next.compactions > prev.compactions && !pend.has(next.id)) {
    pend = new Map(pend).set(next.id, { at: now, samples: 0 });
  }
  hist = pruneSession(hist, next.id, new Set(next.agents.map((a) => a.id)));

  let out: Map<string, readonly HistoryPoint[]> | null = null;
  for (const a of next.agents) {
    if (a.context_tokens === null) continue;
    const key = historyKey(next.id, a.id);
    const list = hist.get(key) ?? [];
    const last = list[list.length - 1];
    if (last && last.tokens === a.context_tokens) continue;
    let drop = false;
    const flag = a.id === "main" ? pend.get(next.id) : undefined;
    if (flag) {
      if (last && a.context_tokens < last.tokens) {
        drop = true;
        pend = without(pend, next.id);
      } else if (flag.samples + 1 >= PENDING_MAX_SAMPLES || now - flag.at > PENDING_MAX_MS) {
        pend = without(pend, next.id);
      } else {
        pend = new Map(pend).set(next.id, { at: flag.at, samples: flag.samples + 1 });
      }
    }
    const grown = [...list, { t: now, tokens: a.context_tokens, drop }];
    if (!out) out = new Map(hist);
    out.set(key, grown.length > HISTORY_CAP ? grown.slice(grown.length - HISTORY_CAP) : grown);
  }
  return { history: out ?? hist, pending: pend };
}

/** Pure reducer. Unknown or empty messages return the same state object. */
export function reduce(state: MissionState, msg: ServerMessage | null, now: number = Date.now()): MissionState {
  if (!msg) return state;
  if (msg.type === "snapshot") {
    const sessions = new Map<string, Session>();
    const present = new Set(msg.sessions.map((s) => s.id));
    let history: History = new Map([...state.history].filter(([k]) => [...present].some((id) => k.startsWith(`${id}/`))));
    let pending: PendingMap = new Map([...state.pending].filter(([id]) => present.has(id)));
    for (const s of msg.sessions) {
      const prev = state.sessions.get(s.id);
      const shared = shareSession(prev, s);
      sessions.set(s.id, shared);
      ({ history, pending } = recordHistory(history, pending, prev, shared, now));
    }
    return { sessions, version: msg.version, history, pending, snapshots: state.snapshots + 1 };
  }
  if (msg.sessions.length === 0) return state;
  const sessions = new Map(state.sessions);
  let history = state.history;
  let pending = state.pending;
  for (const s of msg.sessions) {
    const prev = state.sessions.get(s.id);
    const shared = shareSession(prev, s);
    sessions.set(s.id, shared);
    ({ history, pending } = recordHistory(history, pending, prev, shared, now));
  }
  return { ...state, sessions, history, pending };
}

/** Active sessions first, then ended; newest start first within each group. */
export function orderSessions(sessions: Iterable<Session>): Session[] {
  return [...sessions].sort((a, b) => {
    if (a.status !== b.status) return a.status === "active" ? -1 : 1;
    return b.started - a.started;
  });
}

/** Most recently started active session; falls back to most recent overall. */
export function pickDefaultSession(sessions: Iterable<Session>): string | null {
  return orderSessions(sessions)[0]?.id ?? null;
}

/** Explicit selection wins as long as that session still exists. */
export function resolveSelected(sessions: Sessions, explicitId: string | null): string | null {
  if (explicitId && sessions.has(explicitId)) return explicitId;
  return pickDefaultSession(sessions.values());
}

export interface FeedItem {
  key: string;
  entry: Activity;
}

/** Newest first, with stable keys (activity has no ids). */
export function feedItems(activity: readonly Activity[]): FeedItem[] {
  const keys = activityKeys(activity);
  return activity.map((entry, i) => ({ key: keys[i] as string, entry })).reverse();
}

/**
 * Display names per agent id. Lanes that share a label are numbered by start order
 * ("general-purpose 1", "general-purpose 2"); unique labels stay as they are.
 */
export function laneNames(agents: readonly Agent[]): Map<string, string> {
  const counts = new Map<string, number>();
  for (const a of agents) counts.set(a.label, (counts.get(a.label) ?? 0) + 1);
  const order = agents
    .map((a, i) => ({ a, i }))
    .sort((x, y) => x.a.started - y.a.started || x.i - y.i);
  const seen = new Map<string, number>();
  const out = new Map<string, string>();
  for (const { a } of order) {
    if ((counts.get(a.label) ?? 0) > 1 && a.id !== "main") {
      const n = (seen.get(a.label) ?? 0) + 1;
      seen.set(a.label, n);
      out.set(a.id, `${a.label} ${n}`);
    } else {
      out.set(a.id, a.label);
    }
  }
  return out;
}

/**
 * Activity entries are immutable, so a "running" entry never updates. Show it as running only
 * for a subagent-start entry while that lane is still running or waiting; otherwise neutral.
 */
export function feedRowStatus(entry: Activity, liveAgents: ReadonlySet<string>): Activity["status"] {
  if (entry.status !== "running") return entry.status;
  return entry.kind === "agent" && liveAgents.has(entry.agent_id) ? "running" : "info";
}

export function liveAgentIds(agents: readonly Agent[]): Set<string> {
  return new Set(agents.filter((a) => a.status === "running" || a.status === "waiting").map((a) => a.id));
}

export function agentName(names: ReadonlyMap<string, string>, id: string): string {
  if (id === "main") return "Main";
  return names.get(id) ?? id;
}

/** The session's display name; older servers send none, so fall back to the project folder. */
export function sessionName(s: Pick<Session, "title" | "name">): string {
  const n = s.name?.trim();
  return n ? n : s.title;
}

export const TAB_NAME_MAX = 28;
export const DOC_TITLE_NAME_MAX = 60;

/** Shorten with an ellipsis (counting characters, not UTF-16 units). */
export function truncateName(name: string, max: number = TAB_NAME_MAX): string {
  const chars = [...name];
  return chars.length <= max ? name : `${chars.slice(0, max - 1).join("").trimEnd()}\u2026`;
}

/**
 * Tab text: the session name, truncated to `max` characters. Ended tabs, and tabs that would
 * show the same text as another, get the start time appended.
 */
export function tabLabels(sessions: readonly Session[], max: number = TAB_NAME_MAX): Map<string, string> {
  const shown = new Map(sessions.map((s) => [s.id, max === Infinity ? sessionName(s) : truncateName(sessionName(s), max)]));
  const counts = new Map<string, number>();
  for (const text of shown.values()) counts.set(text, (counts.get(text) ?? 0) + 1);
  const withMinutes = new Map<string, string>();
  for (const s of sessions) {
    const text = shown.get(s.id) ?? s.title;
    const dup = (counts.get(text) ?? 0) > 1;
    withMinutes.set(s.id, s.status === "ended" || dup ? `${text} \u00b7 ${formatClock(s.started)}` : text);
  }
  // Two tabs can still collide after the HH:MM suffix (same name, same minute): use seconds instead.
  const again = new Map<string, number>();
  for (const label of withMinutes.values()) again.set(label, (again.get(label) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const s of sessions) {
    const label = withMinutes.get(s.id) as string;
    out.set(s.id, (again.get(label) ?? 0) > 1 ? `${shown.get(s.id) ?? s.title} \u00b7 ${formatClockSeconds(s.started)}` : label);
  }
  return out;
}

const SOURCE_TEXT: Record<NameSource, string> = {
  custom: "custom name",
  generated: "auto (generated by Claude Code)",
  folder: "folder name",
};

/** Tab tooltip: full name, project folder, start time and where the name came from. */
export function tabTooltip(s: Session): string {
  const source = s.name_source ? SOURCE_TEXT[s.name_source] : "folder name";
  return [sessionName(s), `Project: ${s.cwd || s.title}`, `Started ${formatClock(s.started)}`, `Name: ${source}`].join("\n");
}

export type ElapsedMode = { kind: "live" } | { kind: "fixed"; end: number } | { kind: "unknown" };

/**
 * A call still marked running after its agent finished or its session ended can never
 * complete: clamp it to that end time, or report unknown when there is none.
 */
export function runningCallElapsed(stale: boolean, cap: number | null): ElapsedMode {
  if (!stale) return { kind: "live" };
  return cap === null ? { kind: "unknown" } : { kind: "fixed", end: cap };
}

export function isStale(agent: Agent, sessionEnded: number | null): boolean {
  return sessionEnded !== null || (agent.id !== "main" && (agent.status === "done" || agent.status === "error"));
}

export interface SpawnInfo {
  call: ToolCall;
  /** The agent whose call list contains the spawn call. */
  parentId: string;
}

/** Find the tool call that spawned subagent `subId`, searching every agent's call list. */
export function findSpawn(agents: readonly Agent[], subId: string): SpawnInfo | null {
  for (const a of agents) {
    const call = a.calls.find((c) => c.subagent_id === subId);
    if (call) return { call, parentId: a.id };
  }
  return null;
}

/** Subagents in lane order (Main excluded). */
export function subagentsOf(agents: readonly Agent[]): Agent[] {
  return agents.filter((a) => a.id !== "main");
}

export function findAgent(agents: readonly Agent[], id: string | null): Agent | null {
  return id === null ? null : (agents.find((a) => a.id === id) ?? null);
}

export type CallFilter = "all" | "running" | "errors";

/** Newest first; status chip plus case-insensitive text match on tool and summary. */
export function filterCalls(calls: readonly ToolCall[], status: CallFilter, text: string): ToolCall[] {
  const q = text.trim().toLowerCase();
  return calls
    .filter((c) => (status === "running" ? c.status === "running" : status === "errors" ? c.status === "error" : true))
    .filter((c) => q === "" || c.tool.toLowerCase().includes(q) || c.summary.toLowerCase().includes(q))
    .reverse();
}

export interface HistoryStats {
  min: number;
  max: number;
  current: number;
  count: number;
}

export function historyStats(points: readonly HistoryPoint[]): HistoryStats | null {
  const last = points[points.length - 1];
  if (!last) return null;
  let min = Infinity;
  let max = -Infinity;
  for (const p of points) {
    if (p.tokens < min) min = p.tokens;
    if (p.tokens > max) max = p.tokens;
  }
  return { min, max, current: last.tokens, count: points.length };
}
