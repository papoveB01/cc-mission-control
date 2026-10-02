import { formatClock } from "./format";
import type { Activity, Agent, Session, ToolCall } from "./types";

export type Sessions = ReadonlyMap<string, Session>;

export interface MissionState {
  sessions: Sessions;
  version: string | null;
}

export type ServerMessage =
  | { type: "snapshot"; version: string; sessions: Session[] }
  | { type: "sessions"; sessions: Session[] };

export const initialState: MissionState = { sessions: new Map(), version: null };

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
  const sessions = data.sessions.filter(isSession);
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

/** Pure reducer. Unknown or empty messages return the same state object. */
export function reduce(state: MissionState, msg: ServerMessage | null): MissionState {
  if (!msg) return state;
  if (msg.type === "snapshot") {
    const sessions = new Map<string, Session>();
    for (const s of msg.sessions) sessions.set(s.id, shareSession(state.sessions.get(s.id), s));
    return { sessions, version: msg.version };
  }
  if (msg.sessions.length === 0) return state;
  const sessions = new Map(state.sessions);
  for (const s of msg.sessions) sessions.set(s.id, shareSession(state.sessions.get(s.id), s));
  return { ...state, sessions };
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

/** Tab text: ended tabs and tabs with a duplicate title get the start time appended. */
export function tabLabels(sessions: readonly Session[]): Map<string, string> {
  const counts = new Map<string, number>();
  for (const s of sessions) counts.set(s.title, (counts.get(s.title) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const s of sessions) {
    const dup = (counts.get(s.title) ?? 0) > 1;
    out.set(s.id, s.status === "ended" || dup ? `${s.title} \u00b7 ${formatClock(s.started)}` : s.title);
  }
  return out;
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
