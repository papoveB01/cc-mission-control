import { describe, expect, it } from "vitest";
import { HISTORY_MAX_SPAN, MAX_SPAN, historyTimeline, initialTimeline, niceTicks, timelineReducer } from "./timeline";

const DAY = 86_400;

describe("zoom-out cap by mode", () => {
  const view = { start: 0, span: 3600 };

  it("live stays capped at one day and its state shape is unchanged", () => {
    expect(MAX_SPAN).toBe(DAY);
    expect(timelineReducer(initialTimeline, { type: "zoom", factor: 1e-9, anchor: 0.5, view }).span).toBe(DAY);
    expect(timelineReducer(initialTimeline, { type: "fit", t0: 0, t1: 10 * DAY, live: false }).span).toBe(DAY);
    expect("maxSpan" in timelineReducer(initialTimeline, { type: "pan", dt: 5, view })).toBe(false);
  });

  it("history allows 30 days and keeps its cap through every action", () => {
    expect(HISTORY_MAX_SPAN).toBe(30 * DAY);
    let s = timelineReducer(historyTimeline, { type: "fit", t0: 0, t1: 10 * DAY, live: false });
    expect(s.span).toBeCloseTo(10.6 * DAY);
    expect(s.follow).toBe(false);
    s = timelineReducer(s, { type: "zoom", factor: 1e-9, anchor: 0.5, view });
    expect(s.span).toBe(30 * DAY);
    s = timelineReducer(s, { type: "pan", dt: 10, view });
    expect(s.maxSpan).toBe(HISTORY_MAX_SPAN);
    expect(timelineReducer(s, { type: "reset" })).toEqual(historyTimeline);
    expect(timelineReducer(initialTimeline, { type: "reset" })).toEqual(initialTimeline);
  });
});

describe("multi-day ticks", () => {
  it("uses 2, 7, 14 and 30 day steps with labels at least 96 px apart", () => {
    const utc = (): number => 0;
    for (const [days, step] of [[20, 2], [60, 7], [120, 14], [300, 30]] as const) {
      const ticks = niceTicks({ start: 1_700_000_000, span: days * DAY }, 1000, 96, utc);
      expect(ticks.length).toBeGreaterThan(1);
      const gaps = ticks.slice(1).map((t, i) => t.t - (ticks[i] as { t: number }).t);
      expect(new Set(gaps)).toEqual(new Set([step * DAY]));
      expect(((gaps[0] as number) / (days * DAY)) * 1000).toBeGreaterThanOrEqual(96);
    }
  });
});
