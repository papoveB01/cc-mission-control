import type { HistoryPoint } from "./store";

export interface SparkGeometry {
  line: string;
  /** x positions of compaction markers. */
  drops: number[];
  last: { x: number; y: number } | null;
}

/**
 * Map samples to SVG coordinates. x spans the sampled time range (single samples sit at the
 * right edge), y spans 0..max(window, peak) so the line reads as "how full".
 */
export function sparkGeometry(points: readonly HistoryPoint[], width: number, height: number, window: number | null): SparkGeometry {
  if (points.length === 0) return { line: "", drops: [], last: null };
  const peak = Math.max(window ?? 0, ...points.map((p) => p.tokens), 1);
  const t0 = (points[0] as HistoryPoint).t;
  const span = (points[points.length - 1] as HistoryPoint).t - t0;
  const pad = 2;
  if (points.length === 1) {
    const y = pad + (1 - (points[0] as HistoryPoint).tokens / peak) * (height - 2 * pad);
    const f1 = (n: number): string => n.toFixed(1);
    return { line: `M${f1(pad)} ${f1(y)} L${f1(width - pad)} ${f1(y)}`, drops: [], last: { x: width - pad, y } };
  }
  const xy = points.map((p) => ({
    x: span > 0 ? pad + ((p.t - t0) / span) * (width - 2 * pad) : width - pad,
    y: pad + (1 - p.tokens / peak) * (height - 2 * pad),
    drop: p.drop,
  }));
  const f = (n: number): string => n.toFixed(1);
  return {
    line: xy.map((q, i) => `${i === 0 ? "M" : "L"}${f(q.x)} ${f(q.y)}`).join(" "),
    drops: xy.filter((q) => q.drop).map((q) => q.x),
    last: xy[xy.length - 1] ?? null,
  };
}
