/** 118000 -> "118k", 1200 -> "1.2k", 1000000 -> "1M". */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return `${trim1(n / 1000)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${trim1(n / 1_000_000)}M`;
}

function trim1(x: number): string {
  const s = x.toFixed(1);
  return s.endsWith(".0") ? s.slice(0, -2) : s;
}

/** Milliseconds -> "<0.1s", "2.4s", "41s", "41m", "1h 05m". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  if (ms < 100) return "<0.1s";
  if (ms < 10_000) return `${(Math.floor(ms / 100) / 10).toFixed(1)}s`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

const pad = (n: number): string => String(n).padStart(2, "0");

/** Epoch seconds -> local "HH:MM". */
export function formatClock(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Epoch seconds -> local "HH:MM:SS". */
export function formatClockSeconds(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export type GaugeLevel = "ok" | "warn" | "high";

/** Fraction (0..1+) -> level. Under 60% neutral, 60-80% amber, over 80% red. */
export function gaugeLevel(fraction: number): GaugeLevel {
  if (fraction > 0.8) return "high";
  if (fraction >= 0.6) return "warn";
  return "ok";
}

export function gaugePercent(tokens: number, window: number): number {
  if (window <= 0) return 0;
  return Math.round((tokens / window) * 100);
}

/** 1 -> "1 error", 2 -> "2 errors". */
export function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** Display name for a tool: "mcp__server__action" shows as "action" (the full name goes in a title). */
export function toolLabel(tool: string): string {
  if (!tool.startsWith("mcp__")) return tool;
  const parts = tool.split("__");
  return parts[parts.length - 1] || tool;
}
