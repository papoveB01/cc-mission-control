import type { Activity, ToolCall } from "./types";

export type StatusFilter = "all" | "errors" | "running";

export interface Filters {
  status: StatusFilter;
  agents: string[];
  tools: string[];
  text: string;
}

export const noFilters: Filters = { status: "all", agents: [], tools: [], text: "" };

export const filtersActive = (f: Filters): boolean => f.status !== "all" || f.agents.length > 0 || f.tools.length > 0 || f.text.trim() !== "";

export const activeFilterCount = (f: Filters): number => (f.status !== "all" ? 1 : 0) + f.agents.length + f.tools.length + (f.text.trim() ? 1 : 0);

const has = (list: readonly string[], v: string): boolean => list.includes(v);

/** Does a tool call match the filters? Used for dimming lane rows and timeline bars. */
export function matchCall(f: Filters, call: Pick<ToolCall, "agent_id" | "tool" | "summary" | "status">): boolean {
  if (f.status === "errors" && call.status !== "error") return false;
  if (f.status === "running" && call.status !== "running") return false;
  if (f.agents.length > 0 && !has(f.agents, call.agent_id)) return false;
  if (f.tools.length > 0 && !has(f.tools, call.tool)) return false;
  const q = f.text.trim().toLowerCase();
  return q === "" || call.tool.toLowerCase().includes(q) || call.summary.toLowerCase().includes(q);
}

/** Tool name of a tool activity entry ("Bash: pytest" -> "Bash"), else null. */
export function entryTool(e: Pick<Activity, "kind" | "text">): string | null {
  if (e.kind !== "tool") return null;
  const i = e.text.indexOf(":");
  return (i > 0 ? e.text.slice(0, i) : e.text).trim() || null;
}

/**
 * Does a feed entry match? The feed hides non-matching entries.
 *
 * "Running" means "still in progress right now" everywhere: for calls (lanes, timeline) a call
 * with status running; for feed entries a subagent-start entry whose lane is still running or
 * waiting. Tool entries are logged once at start and never change, so they cannot be "running"
 * in the feed; the chip title says so.
 */
export function matchEntry(f: Filters, e: Activity, liveAgents: ReadonlySet<string>): boolean {
  if (f.status === "errors" && e.status !== "error") return false;
  if (f.status === "running" && !(e.status === "running" && e.kind === "agent" && liveAgents.has(e.agent_id))) return false;
  if (f.agents.length > 0 && !has(f.agents, e.agent_id)) return false;
  if (f.tools.length > 0) {
    const t = entryTool(e);
    if (t === null || !has(f.tools, t)) return false;
  }
  const q = f.text.trim().toLowerCase();
  return q === "" || e.text.toLowerCase().includes(q);
}

export const FILTER_KEY = "ccmc.filters";

interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(0, 200) : []);

/** Parse stored filters defensively; anything unexpected falls back to no filters. */
export function parseFilters(raw: string | null): Filters {
  if (!raw) return noFilters;
  try {
    const v: unknown = JSON.parse(raw);
    if (typeof v !== "object" || v === null) return noFilters;
    const r = v as Record<string, unknown>;
    const status: StatusFilter = r.status === "errors" || r.status === "running" ? r.status : "all";
    return { status, agents: strings(r.agents), tools: strings(r.tools), text: typeof r.text === "string" ? r.text.slice(0, 200) : "" };
  } catch {
    return noFilters;
  }
}

export function loadFilters(storage: StorageLike | null = safeStorage()): Filters {
  try {
    return parseFilters(storage ? storage.getItem(FILTER_KEY) : null);
  } catch {
    return noFilters;
  }
}

export function saveFilters(f: Filters, storage: StorageLike | null = safeStorage()): void {
  try {
    storage?.setItem(FILTER_KEY, JSON.stringify(f));
  } catch {
    /* storage unavailable or full */
  }
}

export function safeStorage(): StorageLike | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function toggleIn(list: readonly string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

/**
 * Drop stored agent and tool ids that do not exist in the current session. The stored filters
 * stay untouched; only the applied ones are pruned. `pruned` counts what was dropped.
 */
export function pruneFilters(f: Filters, agentIds: ReadonlySet<string>, toolNames: ReadonlySet<string>): { filters: Filters; pruned: number } {
  const agents = f.agents.filter((a) => agentIds.has(a));
  const tools = f.tools.filter((t) => toolNames.has(t));
  const pruned = f.agents.length - agents.length + (f.tools.length - tools.length);
  return pruned === 0 ? { filters: f, pruned: 0 } : { filters: { ...f, agents, tools }, pruned };
}
