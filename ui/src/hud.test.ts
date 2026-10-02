import { describe, expect, it } from "vitest";
import { DIAL_SWEEP, dialGeometry } from "./dial";
import { sparkGeometry } from "./spark";
import { HISTORY_CAP, filterCalls, findAgent, findSpawn, historyKey, historyStats, initialState, reduce, subagentsOf, type ServerMessage } from "./store";
import { layoutTopology, MAX_RING_NODES, NODE_R, nextPulses, shortName, splitSubagents } from "./topology";
import type { Agent, Session, ToolCall } from "./types";

function agent(over: Partial<Agent> = {}): Agent {
  return {
    id: "main", label: "Main", agent_type: null, status: "running", task: "", result: "", started: 1, ended: null,
    context_tokens: 1000, context_window: 200000, model: null, tool_counts: {}, errors: 0, total_calls: 0, calls: [], ...over,
  };
}
function session(agents: Agent[], over: Partial<Session> = {}): Session {
  return {
    id: "s", title: "s", cwd: "/s", model: null, status: "active", started: 1, ended: null, last_event: 1,
    compactions: 0, agents, activity: [], ...over,
  };
}
const snap = (s: Session[]): ServerMessage => ({ type: "snapshot", version: "x", sessions: s });
const upd = (s: Session[]): ServerMessage => ({ type: "sessions", sessions: s });

describe("context history recorder", () => {
  it("records a sample only when context_tokens changes", () => {
    let st = reduce(initialState, snap([session([agent()])]), 1000);
    st = reduce(st, upd([session([agent()], { last_event: 2 })]), 2000);
    expect(st.history.get(historyKey("s", "main"))).toHaveLength(1);
    st = reduce(st, upd([session([agent({ context_tokens: 2000 })])]), 3000);
    expect(st.history.get(historyKey("s", "main"))?.map((p) => p.tokens)).toEqual([1000, 2000]);
  });

  it("skips agents without context data and keeps the same map when nothing changed", () => {
    const st = reduce(initialState, snap([session([agent({ context_tokens: null })])]), 1);
    expect(st.history.size).toBe(0);
    const st2 = reduce(st, upd([session([agent({ context_tokens: null })])]), 2);
    expect(st2.history).toBe(st.history);
  });

  it("caps each agent at 300 points, dropping the oldest", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 0 })])]), 0);
    for (let i = 1; i <= HISTORY_CAP + 20; i++) st = reduce(st, upd([session([agent({ context_tokens: i })])]), i);
    const list = st.history.get(historyKey("s", "main")) ?? [];
    expect(list).toHaveLength(HISTORY_CAP);
    expect(list[list.length - 1]?.tokens).toBe(HISTORY_CAP + 20);
    expect(list[0]?.tokens).toBe(21);
  });

  it("marks the compaction drop on the next falling Main sample", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 150000 })])]), 1);
    st = reduce(st, upd([session([agent({ context_tokens: 20000 })], { compactions: 1 })]), 2);
    expect(st.history.get(historyKey("s", "main"))?.map((p) => p.drop)).toEqual([false, true]);
    expect(st.pending.has("s")).toBe(false);
  });

  it("keeps the compaction pending until tokens actually fall", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 5000 })])]), 1);
    st = reduce(st, upd([session([agent({ context_tokens: 5000 })], { compactions: 1 })]), 2);
    expect(st.pending.has("s")).toBe(true);
    st = reduce(st, upd([session([agent({ context_tokens: 6000 })], { compactions: 1 })]), 3);
    expect(st.history.get(historyKey("s", "main"))?.at(-1)?.drop).toBe(false);
    st = reduce(st, upd([session([agent({ context_tokens: 900 })], { compactions: 1 })]), 4);
    expect(st.history.get(historyKey("s", "main"))?.at(-1)?.drop).toBe(true);
    expect(st.pending.has("s")).toBe(false);
  });

  it("does not mark ordinary drops", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 5000 })])]), 1);
    st = reduce(st, upd([session([agent({ context_tokens: 100 })])]), 2);
    expect(st.history.get(historyKey("s", "main"))?.at(-1)?.drop).toBe(false);
  });

  it("resets a session's history when the compaction counter goes down", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 5000 })], { compactions: 3 })]), 1);
    st = reduce(st, upd([session([agent({ context_tokens: 700 })], { compactions: 0 })]), 2);
    expect(st.history.get(historyKey("s", "main"))?.map((p) => p.tokens)).toEqual([700]);
  });

  it("prunes histories of agents and sessions that disappeared", () => {
    const sub = agent({ id: "x", context_tokens: 10 });
    let st = reduce(initialState, snap([session([agent(), sub]), { ...session([agent()]), id: "other" }]), 1);
    expect(st.history.has(historyKey("s", "x"))).toBe(true);
    st = reduce(st, upd([session([agent()])]), 2);
    expect(st.history.has(historyKey("s", "x"))).toBe(false);
    expect(st.history.has(historyKey("other", "main"))).toBe(true);
    st = reduce(st, snap([session([agent()])]), 3);
    expect(st.history.has(historyKey("other", "main"))).toBe(false);
    expect(st.snapshots).toBe(2);
  });
});

describe("topology ring layout", () => {
  const subs = (n: number, status: Agent["status"] = "running"): Agent[] =>
    Array.from({ length: n }, (_, i) => agent({ id: `a${i}`, label: "x", started: i + 2, status }));
  const minDistance = (nodes: { x: number; y: number }[]): number => {
    let m = Infinity;
    for (let i = 0; i < nodes.length; i++)
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i] as { x: number; y: number };
        const b = nodes[j] as { x: number; y: number };
        m = Math.min(m, Math.hypot(a.x - b.x, a.y - b.y));
      }
    return m;
  };

  it("places Main at the centre", () => {
    const l = layoutTopology([agent(), ...subs(3)]);
    const main = l.nodes.find((n) => n.kind === "main");
    expect(main?.x).toBe(l.size / 2);
    expect(main?.y).toBe(l.size / 2);
    expect(l.nodes).toHaveLength(4);
  });

  it("puts finished agents on a dimmed outer ring", () => {
    const l = layoutTopology([agent(), ...subs(2), ...subs(2, "done").map((a, i) => ({ ...a, id: `d${i}` }))]);
    const c = l.size / 2;
    const dist = (id: string): number => {
      const n = l.nodes.find((x) => x.id === id);
      return Math.hypot((n?.x ?? 0) - c, (n?.y ?? 0) - c);
    };
    expect(l.nodes.find((n) => n.id === "d0")?.dim).toBe(true);
    expect(dist("d0")).toBeGreaterThan(dist("a0"));
  });

  it("fits 24 subagents without overlap and without grouping", () => {
    const l = layoutTopology([agent(), ...subs(24)]);
    expect(l.nodes.filter((n) => n.kind === "group")).toHaveLength(0);
    expect(l.nodes).toHaveLength(25);
    expect(minDistance(l.nodes)).toBeGreaterThanOrEqual(2 * NODE_R);
  });

  it("groups finished subagents past 24", () => {
    const all = [...subs(5), ...subs(25, "done").map((a, i) => ({ ...a, id: `d${i}` }))];
    const split = splitSubagents(all);
    expect(split.active).toHaveLength(5);
    expect(split.finished.length + 1).toBeLessThanOrEqual(MAX_RING_NODES - 5);
    expect(split.finished.length + split.grouped.length).toBe(25);
    const l = layoutTopology([agent(), ...all]);
    const group = l.nodes.filter((n) => n.kind === "group");
    expect(group).toHaveLength(1);
    expect(group[0]?.members).toHaveLength(split.grouped.length);
    expect(l.nodes.length - 1).toBeLessThanOrEqual(MAX_RING_NODES);
    expect(minDistance(l.nodes)).toBeGreaterThanOrEqual(2 * NODE_R);
  });
});

describe("dial geometry", () => {
  it("draws 270 degrees and scales the fill", () => {
    const g = dialGeometry(100, 200, 40);
    expect(g.track).toBeCloseTo(2 * Math.PI * 40 * DIAL_SWEEP);
    expect(g.fill).toBeCloseTo(g.track / 2);
    expect(g.fraction).toBe(0.5);
  });
  it("clamps overflow and handles empty windows", () => {
    expect(dialGeometry(300, 200, 40).fill).toBeCloseTo(dialGeometry(300, 200, 40).track);
    expect(dialGeometry(5, 0, 40).fill).toBe(0);
  });
  it("applies the 60/80 thresholds", () => {
    expect(dialGeometry(59, 100, 40).level).toBe("ok");
    expect(dialGeometry(60, 100, 40).level).toBe("warn");
    expect(dialGeometry(80, 100, 40).level).toBe("warn");
    expect(dialGeometry(81, 100, 40).level).toBe("high");
    expect(dialGeometry(120, 100, 40).level).toBe("high");
  });
});

describe("sparkline geometry", () => {
  it("returns nothing for no samples and a dot for one", () => {
    expect(sparkGeometry([], 100, 20, 200).line).toBe("");
    const one = sparkGeometry([{ t: 5, tokens: 100, drop: false }], 100, 20, 200);
    expect(one.last?.x).toBe(98);
  });
  it("draws a flat line across the width for a single sample", () => {
    const g = sparkGeometry([{ t: 5, tokens: 100, drop: false }], 100, 20, 200);
    expect(g.line).toMatch(/^M2\.0 [\d.]+ L98\.0 [\d.]+$/);
    expect(g.last?.x).toBe(98);
  });
  it("reports compaction ticks", () => {
    const g = sparkGeometry(
      [
        { t: 0, tokens: 100, drop: false },
        { t: 10, tokens: 10, drop: true },
      ],
      100, 20, 200,
    );
    expect(g.drops).toHaveLength(1);
    expect(g.line.startsWith("M")).toBe(true);
  });
});

describe("topology helpers", () => {
  it("keeps the newest finished agents and folds the oldest", () => {
    const fin = Array.from({ length: 26 }, (_, i) => agent({ id: `d${i}`, status: "done", started: i, ended: 100 + i }));
    const split = splitSubagents(fin);
    expect(split.finished.map((a) => a.id)).toContain("d25");
    expect(split.grouped.map((a) => a.id)).toContain("d0");
    expect(split.grouped.map((a) => a.id)).not.toContain("d25");
  });
  it("builds short labels with the lane number", () => {
    expect(shortName("general-purpose", "general-purpose 2")).toBe("GP2");
    expect(shortName("code-reviewer", "code-reviewer 1")).toBe("CR1");
    expect(shortName("Explore", "Explore")).toBe("E");
    expect(shortName("Explore", "Explore 3")).toBe("E3");
  });
  it("pulses only on increases after a baseline", () => {
    const a = [agent({ id: "a", total_calls: 2 }), agent({ id: "b", total_calls: 0 })];
    const base = nextPulses(null, a);
    expect(base.pulsed).toEqual([]);
    const next = nextPulses(base.counts, [agent({ id: "a", total_calls: 3 }), agent({ id: "b", total_calls: 0 }), agent({ id: "c", total_calls: 4 }), agent({ id: "d", total_calls: 0 })]);
    expect(next.pulsed.sort()).toEqual(["a", "c"]);
    expect(nextPulses(next.counts, [agent({ id: "a", total_calls: 3 })]).pulsed).toEqual([]);
  });
});

describe("agent selection and lineage helpers", () => {
  const call = (id: string, over: Partial<ToolCall> = {}): ToolCall => ({
    id, agent_id: "main", tool: "Bash", summary: "pytest -q", status: "ok", started: 1, ended: 2, duration_ms: 1, subagent_id: null, ...over,
  });
  const agents = [
    agent({ calls: [call("c1"), call("c2", { tool: "Agent", summary: "Map callers", subagent_id: "sub1" })] }),
    agent({ id: "sub1", label: "Explore" }),
  ];
  it("finds agents and subagents", () => {
    expect(findAgent(agents, "sub1")?.label).toBe("Explore");
    expect(findAgent(agents, "nope")).toBeNull();
    expect(findAgent(agents, null)).toBeNull();
    expect(subagentsOf(agents).map((a) => a.id)).toEqual(["sub1"]);
  });
  it("links a subagent to its spawn call in any agent's call list", () => {
    const spawn = findSpawn(agents, "sub1");
    expect(spawn?.call.id).toBe("c2");
    expect(spawn?.parentId).toBe("main");
    expect(findSpawn(agents, "missing")).toBeNull();
  });
});

describe("modal call filtering", () => {
  const mk = (id: string, status: ToolCall["status"], tool: string, summary: string): ToolCall => ({
    id, agent_id: "main", tool, summary, status, started: 1, ended: null, duration_ms: null, subagent_id: null,
  });
  const calls = [mk("1", "ok", "Bash", "pytest -q"), mk("2", "error", "Bash", "make build"), mk("3", "running", "Read", "/src/app.py")];
  it("is newest first and does not mutate the input", () => {
    expect(filterCalls(calls, "all", "").map((c) => c.id)).toEqual(["3", "2", "1"]);
    expect(calls[0]?.id).toBe("1");
  });
  it("filters by status chip", () => {
    expect(filterCalls(calls, "errors", "").map((c) => c.id)).toEqual(["2"]);
    expect(filterCalls(calls, "running", "").map((c) => c.id)).toEqual(["3"]);
  });
  it("filters by text on tool and summary, case-insensitively, combined with status", () => {
    expect(filterCalls(calls, "all", "BASH").map((c) => c.id)).toEqual(["2", "1"]);
    expect(filterCalls(calls, "all", "app.py").map((c) => c.id)).toEqual(["3"]);
    expect(filterCalls(calls, "errors", "pytest")).toEqual([]);
  });
  it("computes history stats", () => {
    expect(historyStats([])).toBeNull();
    expect(historyStats([{ t: 1, tokens: 5, drop: false }, { t: 2, tokens: 9, drop: false }, { t: 3, tokens: 2, drop: true }])).toEqual({ min: 2, max: 9, current: 2, count: 3 });
  });
});
