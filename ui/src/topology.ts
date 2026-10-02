import type { Agent } from "./types";

export const MAX_RING_NODES = 24;
export const NODE_R = 14;
export const MAIN_R = 22;
const GAP = 10;

export interface TopoNode {
  id: string;
  kind: "main" | "sub" | "group";
  x: number;
  y: number;
  r: number;
  dim: boolean;
  /** For a group node, the grouped agent ids. */
  members: string[];
}

export interface TopoLayout {
  /** Side of the square viewBox, centered on (size/2, size/2). */
  size: number;
  nodes: TopoNode[];
}

const isFinished = (a: Agent): boolean => a.status === "done" || a.status === "error";

/** Radius needed so `n` nodes of radius NODE_R sit on a ring without touching. */
export function ringRadius(n: number, min: number): number {
  const chord = 2 * NODE_R + GAP;
  return Math.max(min, (n * chord) / (2 * Math.PI));
}

/**
 * Decide which subagents are drawn individually. Past 24 subagents, the finished ones that do
 * not fit are folded into one "+N finished" node (kept to MAX_RING_NODES nodes in total).
 */
export function splitSubagents(subs: readonly Agent[]): { active: Agent[]; finished: Agent[]; grouped: Agent[] } {
  const active = subs.filter((a) => !isFinished(a));
  // Newest finished first, so the oldest are the ones folded into the group.
  const finishedAll = subs.filter(isFinished).sort((a, b) => (b.ended ?? b.started) - (a.ended ?? a.started));
  if (subs.length <= MAX_RING_NODES) return { active, finished: finishedAll, grouped: [] };
  const room = Math.max(0, MAX_RING_NODES - active.length - 1);
  return { active, finished: finishedAll.slice(0, room), grouped: finishedAll.slice(room) };
}

function place(items: { id: string; started: number }[], radius: number, cx: number, offset: number): { id: string; x: number; y: number }[] {
  const sorted = [...items].sort((a, b) => a.started - b.started);
  return sorted.map((it, i) => {
    const angle = -Math.PI / 2 + offset + (2 * Math.PI * i) / sorted.length;
    return { id: it.id, x: cx + radius * Math.cos(angle), y: cx + radius * Math.sin(angle) };
  });
}

/** Pure ring layout: Main in the centre, live subagents on the inner ring, finished on the outer. */
export function layoutTopology(agents: readonly Agent[]): TopoLayout {
  const main = agents.find((a) => a.id === "main");
  const subs = agents.filter((a) => a.id !== "main");
  const { active, finished, grouped } = splitSubagents(subs);
  const outerItems: { id: string; started: number; group?: boolean }[] = finished.map((a) => ({ id: a.id, started: a.started }));
  if (grouped.length > 0) outerItems.push({ id: "group", started: Infinity, group: true });

  const inner = active.length;
  const innerR = inner > 0 ? ringRadius(inner, 64) : 0;
  const outerMin = inner > 0 ? innerR + 2 * NODE_R + GAP : 64;
  const outerR = outerItems.length > 0 ? ringRadius(outerItems.length, outerMin) : 0;
  const extent = Math.max(MAIN_R, innerR + NODE_R, outerR + NODE_R) + 12;
  const size = Math.max(160, Math.ceil(extent * 2));
  const cx = size / 2;

  const nodes: TopoNode[] = [];
  if (main) nodes.push({ id: main.id, kind: "main", x: cx, y: cx, r: MAIN_R, dim: false, members: [] });
  for (const p of place(active, innerR, cx, 0)) nodes.push({ ...p, kind: "sub", r: NODE_R, dim: false, members: [] });
  const outerStep = outerItems.length > 0 ? Math.PI / outerItems.length : 0;
  for (const p of place(outerItems, outerR, cx, outerStep)) {
    if (p.id === "group") nodes.push({ ...p, kind: "group", r: NODE_R, dim: true, members: grouped.map((a) => a.id) });
    else nodes.push({ ...p, kind: "sub", r: NODE_R, dim: true, members: [] });
  }
  return { size, nodes };
}

export function contextFraction(a: Pick<Agent, "context_tokens" | "context_window">): number | null {
  if (a.context_tokens === null || !a.context_window || a.context_window <= 0) return null;
  return a.context_tokens / a.context_window;
}

/** Short node label: initials of hyphenated types ("code-reviewer" -> "CR"), else the first letter, plus the lane number. */
export function shortName(label: string, numbered: string): string {
  const words = label.split(/[-_\s]+/).filter(Boolean);
  const base = words.length > 1 ? words.map((w) => w.charAt(0)).join("") : label.charAt(0);
  const num = numbered.startsWith(label) ? numbered.slice(label.length).trim() : "";
  return `${base.toUpperCase().slice(0, 2)}${/^\d+$/.test(num) ? num : ""}`;
}

/**
 * Which nodes pulse after an update. `prev` is null for the baseline (first render, session
 * switch, snapshot/reconnect): nothing pulses. A node pulses when its call count rose; a node
 * that appeared in a live update pulses only if it already carries calls.
 */
export function nextPulses(prev: ReadonlyMap<string, number> | null, agents: readonly Agent[]): { counts: Map<string, number>; pulsed: string[] } {
  const counts = new Map(agents.map((a) => [a.id, a.total_calls]));
  if (!prev) return { counts, pulsed: [] };
  const pulsed = agents.filter((a) => a.total_calls > (prev.get(a.id) ?? 0)).map((a) => a.id);
  return { counts, pulsed };
}
