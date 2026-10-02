import { describe, expect, it } from "vitest";
import { formatClock, formatClockSeconds, formatDuration, formatTokens, gaugeLevel, gaugePercent, plural } from "./format";

describe("formatTokens", () => {
  it("formats", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(1200)).toBe("1.2k");
    expect(formatTokens(118_000)).toBe("118k");
    expect(formatTokens(200_000)).toBe("200k");
    expect(formatTokens(1_000_000)).toBe("1M");
    expect(formatTokens(1_500_000)).toBe("1.5M");
  });
});

describe("formatDuration", () => {
  it("formats", () => {
    expect(formatDuration(40)).toBe("<0.1s");
    expect(formatDuration(100)).toBe("0.1s");
    expect(formatDuration(2400)).toBe("2.4s");
    expect(formatDuration(41_000)).toBe("41s");
    expect(formatDuration(41 * 60_000)).toBe("41m");
    expect(formatDuration(65 * 60_000)).toBe("1h 05m");
    expect(formatDuration(-5)).toBe("<0.1s");
  });
});

describe("clock formats", () => {
  it("uses local time with zero padding", () => {
    const d = new Date(2026, 0, 2, 3, 4, 5);
    expect(formatClock(d.getTime() / 1000)).toBe("03:04");
    expect(formatClockSeconds(d.getTime() / 1000)).toBe("03:04:05");
  });
});

describe("gauge", () => {
  it("applies thresholds", () => {
    expect(gaugeLevel(0.59)).toBe("ok");
    expect(gaugeLevel(0.6)).toBe("warn");
    expect(gaugeLevel(0.8)).toBe("warn");
    expect(gaugeLevel(0.81)).toBe("high");
    expect(gaugeLevel(1.2)).toBe("high");
  });
  it("computes percent", () => {
    expect(gaugePercent(118_000, 200_000)).toBe(59);
    expect(gaugePercent(1, 0)).toBe(0);
  });
});

describe("plural", () => {
  it("uses singular for 1", () => {
    expect(plural(0, "error")).toBe("0 errors");
    expect(plural(1, "error")).toBe("1 error");
    expect(plural(1, "compaction")).toBe("1 compaction");
    expect(plural(2, "call")).toBe("2 calls");
  });
});
