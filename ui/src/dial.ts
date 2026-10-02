import { gaugeLevel, type GaugeLevel } from "./format";

/** The dial is a 270 degree arc opening at the bottom. */
export const DIAL_SWEEP = 0.75;
export const DIAL_ROTATE_DEG = 135;

export interface DialGeometry {
  circumference: number;
  /** Length of the full 270 degree track. */
  track: number;
  /** Length of the filled part. */
  fill: number;
  fraction: number;
  level: GaugeLevel;
}

/** Stroke-dash geometry for a dial of radius `r`. `fraction` is clamped to 0..1 for drawing. */
export function dialGeometry(tokens: number, window: number, r: number): DialGeometry {
  const raw = window > 0 ? tokens / window : 0;
  const fraction = Math.min(1, Math.max(0, raw));
  const circumference = 2 * Math.PI * r;
  const track = circumference * DIAL_SWEEP;
  return { circumference, track, fill: track * fraction, fraction, level: gaugeLevel(raw) };
}
