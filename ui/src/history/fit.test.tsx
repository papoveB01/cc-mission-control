// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HudView } from "../components/HudView";
import type { Agent, Session } from "../types";

const T0 = 1_760_000_000;
const calls = Array.from({ length: 30 }, (_, i) => ({
  id: `c${i}`, agent_id: "main", tool: "Read", summary: "a.py", status: "ok" as const,
  started: T0 + i * 10, ended: T0 + i * 10 + 2, duration_ms: 2000, subagent_id: null,
}));
const agent: Agent = {
  id: "main", label: "Main", agent_type: null, status: "done", task: "t", result: "", started: T0, ended: T0 + 300,
  context_tokens: 1, context_window: 200000, model: null, tool_counts: {}, errors: 0, total_calls: 30, calls,
};
const session: Session = {
  id: "s", title: "p", cwd: "/p", model: null, status: "ended", started: T0, ended: T0 + 302, last_event: T0 + 302,
  compactions: 0, agents: [agent], activity: [],
};

describe("history timeline opens fitted to the whole session", () => {
  beforeEach(() => {
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    };
    (globalThis as { CSS?: unknown }).CSS = { escape: (s: string) => s };
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows the first and the last call, not a default window", () => {
    render(<HudView session={session} live={false} callDetailUrl={() => "/x"} renderHeader={() => <header />} />);
    // Every call is inside the window: all 30 bars are drawn (an unfitted 120 s window shows about 12).
    expect(document.querySelectorAll(".tl-bar").length).toBe(30);
    expect(document.querySelectorAll(".tl-tick-label").length).toBeGreaterThan(0);
    // and no live control is offered
    expect([...document.querySelectorAll("button")].some((b) => b.textContent === "Follow live")).toBe(false);
  });
});
