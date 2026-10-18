import { agentName } from "../store";
import type { Agent } from "../types";
import type { UsageCounts } from "./types";

export interface UsageRow {
  id: string;
  name: string;
  input: number;
  cache_creation: number;
  cache_read: number;
  output: number;
  messages: number;
  /** input + cache_creation + cache_read + output. */
  total: number;
}

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0);

export function usageRow(id: string, name: string, u: Partial<UsageCounts> | undefined): UsageRow {
  const input = num(u?.input);
  const cache_creation = num(u?.cache_creation);
  const cache_read = num(u?.cache_read);
  const output = num(u?.output);
  return { id, name, input, cache_creation, cache_read, output, messages: num(u?.messages), total: input + cache_creation + cache_read + output };
}

/** Total tokens of a session-list row or any object with the four counters. */
export function totalTokens(r: { tokens_in: number; tokens_cache_create: number; tokens_cache_read: number; tokens_out: number }): number {
  return num(r.tokens_in) + num(r.tokens_cache_create) + num(r.tokens_cache_read) + num(r.tokens_out);
}

/**
 * Per-agent rows (session lane order, then any agent only seen in usage) and a total row.
 * The server's `total` entry is used when present, so it matches what the list shows.
 */
export function buildUsage(
  usage: Record<string, Partial<UsageCounts>> | undefined,
  agents: readonly Pick<Agent, "id">[],
  names: ReadonlyMap<string, string>,
): { rows: UsageRow[]; total: UsageRow } {
  const src = usage ?? {};
  const ids = [...agents.map((a) => a.id)];
  for (const id of Object.keys(src)) if (id !== "total" && !ids.includes(id)) ids.push(id);
  const rows = ids.filter((id) => src[id] !== undefined).map((id) => usageRow(id, agentName(names, id), src[id]));
  const sum = rows.reduce(
    (t, r) => ({ ...t, input: t.input + r.input, cache_creation: t.cache_creation + r.cache_creation, cache_read: t.cache_read + r.cache_read, output: t.output + r.output, messages: t.messages + r.messages }),
    { input: 0, cache_creation: 0, cache_read: 0, output: 0, messages: 0 },
  );
  const total = usageRow("total", "Total", src.total ?? sum);
  return { rows, total };
}

/** Widths (percent of the widest agent) for the stacked bar of one row. */
export function barSegments(r: UsageRow, max: number): { key: "input" | "cache_creation" | "cache_read" | "output"; pct: number }[] {
  if (max <= 0) return [];
  return (["input", "cache_creation", "cache_read", "output"] as const).map((key) => ({ key, pct: (r[key] / max) * 100 }));
}
