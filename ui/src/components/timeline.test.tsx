// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { noFilters } from "../filters";
import { laneNames } from "../store";
import { initialTimeline } from "../timeline";
import type { Agent, Session, ToolCall } from "../types";
import { Timeline, type TimelineInfo } from "./Timeline";

// Synthetic fixture shaped like a long real session (no real content): unique, non-numbered
// labels ("Plan"), numbered duplicates, a long span and many calls with very long summaries.
const T0 = 1_700_000_000;
function makeCalls(agentId: string, n: number, offset: number): ToolCall[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `${agentId}-c${i}`,
    agent_id: agentId,
    tool: i % 7 === 0 ? "mcp__synthetic_server__SendMessage" : i % 5 === 0 ? "AskUserQuestion" : "Bash",
    summary: `cd /synthetic/path/${"segment/".repeat(40)} && run --flag=${"x".repeat(300)}`,
    status: i % 11 === 0 ? ("error" as const) : ("ok" as const),
    started: T0 + offset + i * 20,
    ended: T0 + offset + i * 20 + 3,
    duration_ms: 3000,
    subagent_id: null,
  }));
}
function agent(id: string, label: string, n: number, offset: number, over: Partial<Agent> = {}): Agent {
  return {
    id, label, agent_type: id === "main" ? null : label, status: "done", task: "t", result: "", started: T0 + offset, ended: T0 + offset + n * 20,
    context_tokens: 1000, context_window: 200000, model: null, tool_counts: {}, errors: 0, total_calls: n, calls: makeCalls(id, n, offset), ...over,
  };
}
const agents: Agent[] = [
  agent("main", "Main", 60, 0, { status: "idle", ended: null }),
  agent("a-plan", "Plan", 43, 100),
  agent("a-gp1", "general-purpose", 60, 400),
  agent("a-gp2", "general-purpose", 60, 500),
  agent("a-gp3", "general-purpose", 45, 600),
  agent("a-gp4", "general-purpose", 57, 700),
];
const session: Session = {
  id: "synthetic", title: "synthetic", cwd: "/synthetic", model: null, status: "ended", started: T0, ended: T0 + 86_400, last_event: T0 + 86_400,
  compactions: 0, agents, activity: [],
};

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe(): void {}
    disconnect(): void {}
    unobserve(): void {}
  };
  (globalThis as { CSS?: unknown }).CSS = { escape: (s: string) => s };
});
afterEach(cleanup);

function renderTimeline() {
  const names = laneNames(agents);
  const infoRef: { current: TimelineInfo | null } = { current: null };
  return render(
    <Timeline
      session={session}
      names={names}
      filters={noFilters}
      state={{ ...initialTimeline, follow: false, auto: false, start: T0, span: 3600 }}
      dispatch={() => {}}
      fitPending={false}
      onFitDone={() => {}}
      infoRef={infoRef}
      reveal={null}
      onOpenCall={() => {}}
      onOpenAgent={() => {}}
    />,
  );
}

describe("Timeline with a long, busy session (regression)", () => {
  it("renders one labelled row per agent, in lane order, with unique labels like Plan", () => {
    const { container } = renderTimeline();
    const labels = [...container.querySelectorAll(".tl-label")].map((l) => l.textContent);
    expect(labels).toEqual(["Main", "Plan", "general-purpose 1", "general-purpose 2", "general-purpose 3", "general-purpose 4"]);
    const rows = [...container.querySelectorAll<HTMLElement>(".tl-row")];
    expect(rows.map((r) => r.dataset.agentRow)).toEqual(agents.map((a) => a.id));
  });

  it("draws every bar in its own agent's row", () => {
    const { container } = renderTimeline();
    for (const row of container.querySelectorAll<HTMLElement>(".tl-row")) {
      const bars = [...row.querySelectorAll<SVGGElement>(".tl-bar")];
      for (const b of bars) expect(b.dataset.agentId).toBe(row.dataset.agentRow);
    }
    const withBars = [...container.querySelectorAll(".tl-row")].filter((r) => r.querySelector(".tl-bar")).length;
    expect(withBars).toBeGreaterThan(1);
  });

  it("keeps the text alternative inside a clipped wrapper, never a bare .sr-only table", () => {
    // `overflow`/`clip-path` do not clip a table box: with long nowrap summaries it stretched the
    // dock sideways and scrolled the row labels out of view.
    const { container } = renderTimeline();
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.classList.contains("sr-only")).toBe(false);
    const wrapper = table?.closest(".sr-only");
    expect(wrapper?.tagName).toBe("DIV");
    expect(wrapper?.contains(table)).toBe(true);
  });

  it("caps the text alternative at the newest 200 calls and says so", () => {
    const { container } = renderTimeline();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(200);
    expect(container.querySelector("caption")?.textContent).toContain("newest 200 of 325 calls");
  });
});
