import { rankScored } from "./fuzzy";
import { sessionName, tabLabels } from "./store";
import type { Session, ToolCall } from "./types";

export type ActionId = "timeline" | "follow" | "fit" | "clear-filters" | "next-error" | "shortcuts";

export type PaletteTarget =
  | { type: "session"; id: string }
  | { type: "agent"; id: string }
  | { type: "call"; call: ToolCall }
  | { type: "action"; id: ActionId };

export interface PaletteItem {
  id: string;
  label: string;
  detail: string;
  /** Text matched against the query when it differs from the label. */
  search?: string;
  target: PaletteTarget;
}

export interface PaletteGroup {
  heading: string;
  items: PaletteItem[];
}

export const ACTIONS: { id: ActionId; label: string; detail: string }[] = [
  { id: "timeline", label: "Toggle timeline", detail: "T" },
  { id: "follow", label: "Follow live", detail: "Timeline" },
  { id: "fit", label: "Fit timeline", detail: "Timeline" },
  { id: "clear-filters", label: "Clear filters", detail: "Feed" },
  { id: "next-error", label: "Jump to next error", detail: "E" },
  { id: "shortcuts", label: "Show keyboard shortcuts", detail: "?" },
];

export const GROUP_LIMIT = 6;
export const RECENT_CALLS = 60;

export interface PaletteContext {
  sessions: readonly Session[];
  selectedId: string | null;
  names: ReadonlyMap<string, string>;
}

/** Recent calls of the selected session, newest first. */
export function recentCalls(session: Pick<Session, "agents"> | null, limit = RECENT_CALLS): ToolCall[] {
  if (!session) return [];
  return session.agents
    .flatMap((a) => a.calls)
    .sort((a, b) => b.started - a.started)
    .slice(0, limit);
}

/** Grouped, ranked palette results. Empty groups are dropped. */
export function buildResults(query: string, ctx: PaletteContext): PaletteGroup[] {
  const session = ctx.sessions.find((s) => s.id === ctx.selectedId) ?? null;
  const labels = tabLabels(ctx.sessions, Infinity);
  const limit = query.trim() === "" ? 4 : GROUP_LIMIT;

  const actions: PaletteItem[] = ACTIONS.map((a) => ({ id: `action:${a.id}`, label: a.label, detail: a.detail, target: { type: "action", id: a.id } }));
  const sessions: PaletteItem[] = ctx.sessions.map((s) => ({
    id: `session:${s.id}`,
    label: labels.get(s.id) ?? sessionName(s),
    detail: `${s.title} \u00b7 ${s.status === "active" ? "Active" : "Ended"}`,
    search: `${sessionName(s)} ${s.title}`,
    target: { type: "session", id: s.id },
  }));
  const agents: PaletteItem[] = (session?.agents ?? []).map((a) => {
    const name = a.id === "main" ? "Main" : (ctx.names.get(a.id) ?? a.label);
    return { id: `agent:${a.id}`, label: name, detail: a.status, target: { type: "agent", id: a.id } };
  });
  const calls: PaletteItem[] = recentCalls(session).map((c) => ({
    id: `call:${c.id}`,
    label: `${c.tool} ${c.summary}`.slice(0, 120),
    detail: `${c.agent_id === "main" ? "Main" : (ctx.names.get(c.agent_id) ?? c.agent_id)} · ${c.status}`,
    target: { type: "call", call: c },
  }));

  const empty = query.trim() === "";
  const build = (heading: string, items: PaletteItem[], limit: number): { group: PaletteGroup; best: number } => {
    const ranked = rankScored(items, query, (i) => i.search ?? i.label).slice(0, limit);
    return { group: { heading, items: ranked.map((r) => r.item) }, best: ranked[0]?.score ?? -Infinity };
  };
  const built = [
    build("Actions", actions, limit),
    build("Agents", agents, limit),
    build("Sessions", sessions, limit),
    build("Recent calls", calls, empty ? 3 : GROUP_LIMIT),
  ].filter((b) => b.group.items.length > 0);
  // With a query, the group holding the best match comes first (stable otherwise).
  if (!empty) built.sort((a, b) => b.best - a.best);
  return built.map((b) => b.group);
}

export const flatten = (groups: readonly PaletteGroup[]): PaletteItem[] => groups.flatMap((g) => g.items);
