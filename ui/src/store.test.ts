import { describe, expect, it } from "vitest";
import {
  feedItems,
  feedRowStatus,
  liveAgentIds,
  isStale,
  laneNames,
  runningCallElapsed,
  tabLabels,
  shareSession,
  initialState,
  orderSessions,
  parseMessage,
  pickDefaultSession,
  reduce,
  resolveSelected,
} from "./store";
import type { Activity, Agent, Session } from "./types";

function agent(over: Partial<Agent> = {}): Agent {
  return {
    id: "main", label: "Main", agent_type: null, status: "running", task: "t", result: "",
    started: 1, ended: null, context_tokens: 10, context_window: 200000, model: null,
    tool_counts: { Bash: 1 }, errors: 0, total_calls: 1, calls: [], ...over,
  };
}
function session(id: string, over: Partial<Session> = {}): Session {
  return {
    id, title: id, cwd: "/x/" + id, model: null, status: "active", started: 100, ended: null,
    last_event: 100, compactions: 0, agents: [agent()], activity: [], ...over,
  };
}
const snapshot = (sessions: Session[]) => parseMessage(JSON.stringify({ type: "snapshot", version: "0.1.0", sessions }));
const update = (sessions: Session[]) => parseMessage(JSON.stringify({ type: "sessions", sessions }));

describe("reducer", () => {
  it("applies a snapshot", () => {
    const s = reduce(initialState, snapshot([session("a"), session("b")]));
    expect([...s.sessions.keys()]).toEqual(["a", "b"]);
    expect(s.version).toBe("0.1.0");
  });

  it("snapshot replaces the whole set", () => {
    const s1 = reduce(initialState, snapshot([session("a"), session("b")]));
    const s2 = reduce(s1, snapshot([session("b")]));
    expect([...s2.sessions.keys()]).toEqual(["b"]);
  });

  it("partial updates replace by id and keep others", () => {
    const s1 = reduce(initialState, snapshot([session("a"), session("b")]));
    const s2 = reduce(s1, update([session("a", { compactions: 3 }), session("c")]));
    expect(s2.sessions.get("a")?.compactions).toBe(3);
    expect(s2.sessions.get("b")).toBe(s1.sessions.get("b"));
    expect(s2.sessions.has("c")).toBe(true);
  });

  it("keeps identity of unchanged agents and calls", () => {
    const call = { id: "c1", agent_id: "main", tool: "Bash", summary: "ls", status: "ok" as const, started: 1, ended: 2, duration_ms: 1000, subagent_id: null };
    const a2 = agent({ id: "sub", label: "Explore" });
    const s1 = reduce(initialState, snapshot([session("a", { agents: [agent({ calls: [call] }), a2] })]));
    const next = session("a", { agents: [agent({ calls: [{ ...call }], total_calls: 2 }), { ...a2 }] });
    const s2 = reduce(s1, update([next]));
    const before = s1.sessions.get("a");
    const after = s2.sessions.get("a");
    expect(after?.agents[1]).toBe(before?.agents[1]);
    expect(after?.agents[0]).not.toBe(before?.agents[0]);
    expect(after?.agents[0]?.calls[0]).toBe(before?.agents[0]?.calls[0]);
  });

  it("returns the same state for an identical update", () => {
    const s1 = reduce(initialState, snapshot([session("a")]));
    const s2 = reduce(s1, update([session("a")]));
    expect(s2.sessions.get("a")).toBe(s1.sessions.get("a"));
  });

  it("ignores unknown or malformed messages", () => {
    const s1 = reduce(initialState, snapshot([session("a")]));
    expect(parseMessage('{"type":"mystery","sessions":[]}')).toBeNull();
    expect(parseMessage("not json")).toBeNull();
    expect(parseMessage('{"type":"sessions"}')).toBeNull();
    expect(parseMessage('"pong"')).toBeNull();
    expect(reduce(s1, parseMessage('{"type":"mystery"}'))).toBe(s1);
    expect(reduce(s1, update([]))).toBe(s1);
  });

  it("drops sessions that fail validation", () => {
    const msg = parseMessage(JSON.stringify({ type: "sessions", sessions: [{ id: 1 }, session("ok")] }));
    expect(msg?.sessions.map((s) => s.id)).toEqual(["ok"]);
  });
});

describe("session selection", () => {
  const sessions = [
    session("old-active", { started: 10 }),
    session("new-active", { started: 50 }),
    session("newest-ended", { started: 90, status: "ended" }),
  ];
  it("orders active first, newest first", () => {
    expect(orderSessions(sessions).map((s) => s.id)).toEqual(["new-active", "old-active", "newest-ended"]);
  });
  it("defaults to the most recently started active session", () => {
    expect(pickDefaultSession(sessions)).toBe("new-active");
  });
  it("falls back to the newest ended session when none are active", () => {
    expect(pickDefaultSession([session("a", { status: "ended", started: 1 }), session("b", { status: "ended", started: 2 })])).toBe("b");
    expect(pickDefaultSession([])).toBeNull();
  });
  it("keeps the explicit selection, even if it ended", () => {
    const map = new Map(sessions.map((s) => [s.id, s]));
    expect(resolveSelected(map, "newest-ended")).toBe("newest-ended");
    expect(resolveSelected(map, "gone")).toBe("new-active");
    expect(resolveSelected(map, null)).toBe("new-active");
  });
});

describe("feedItems", () => {
  const act = (t: number, text: string): Activity => ({ t, agent_id: "main", kind: "tool", text, status: "info" });
  it("is newest first", () => {
    expect(feedItems([act(1, "a"), act(2, "b"), act(3, "c")]).map((i) => i.entry.text)).toEqual(["c", "b", "a"]);
  });
  it("gives duplicate entries distinct stable keys", () => {
    const items = feedItems([act(1, "x"), act(1, "x")]);
    expect(new Set(items.map((i) => i.key)).size).toBe(2);
    const more = feedItems([act(1, "x"), act(1, "x"), act(2, "y")]);
    expect(more.map((i) => i.key)).toContain(items[0]?.key);
  });
});

describe("laneNames", () => {
  it("numbers duplicate labels by start order and leaves unique ones", () => {
    const names = laneNames([
      agent(),
      agent({ id: "c", label: "general-purpose", started: 30 }),
      agent({ id: "a", label: "general-purpose", started: 10 }),
      agent({ id: "b", label: "general-purpose", started: 20 }),
      agent({ id: "e", label: "Explore", started: 5 }),
    ]);
    expect(names.get("a")).toBe("general-purpose 1");
    expect(names.get("b")).toBe("general-purpose 2");
    expect(names.get("c")).toBe("general-purpose 3");
    expect(names.get("e")).toBe("Explore");
    expect(names.get("main")).toBe("Main");
  });
});

describe("tabLabels", () => {
  it("appends the start time for ended and duplicate titles only", () => {
    const labels = tabLabels([
      session("u", { title: "solo" }),
      session("d1", { title: "dup" }),
      session("d2", { title: "dup" }),
      session("e", { title: "old", status: "ended" }),
    ]);
    expect(labels.get("u")).toBe("solo");
    expect(labels.get("d1")).toMatch(/^dup \u00b7 \d\d:\d\d$/);
    expect(labels.get("e")).toMatch(/^old \u00b7 \d\d:\d\d$/);
  });
});

describe("stuck running calls", () => {
  it("ticks only while the agent and session are live", () => {
    expect(isStale(agent({ id: "s", status: "running" }), null)).toBe(false);
    expect(runningCallElapsed(false, null)).toEqual({ kind: "live" });
  });
  it("clamps to the end time once the agent finished or session ended", () => {
    expect(isStale(agent({ id: "s", status: "done" }), null)).toBe(true);
    expect(isStale(agent(), 500)).toBe(true);
    expect(runningCallElapsed(true, 500)).toEqual({ kind: "fixed", end: 500 });
  });
  it("reports unknown when there is no end time", () => {
    expect(runningCallElapsed(true, null)).toEqual({ kind: "unknown" });
  });
});

describe("activity sharing", () => {
  const row = (i: number, text = `r${i}`): Activity => ({ t: i, agent_id: "main", kind: "tool", text, status: "info" });
  it("keeps identity of surviving rows when the oldest rows roll off", () => {
    const rows = [0, 1, 2, 3, 4].map((i) => row(i));
    const a = session("a", { activity: rows });
    const incoming = [...rows.slice(1), row(9, "new")].map((r) => ({ ...r }));
    const b = shareSession(a, session("a", { activity: incoming }));
    expect(b.activity[0]).toBe(rows[1]);
    expect(b.activity[3]).toBe(rows[4]);
  });
  it("counts duplicates from the newest end so keys survive roll-off", () => {
    const dup = row(1, "x");
    const full = feedItems([dup, dup, dup]);
    const rolled = feedItems([dup, dup]);
    expect(rolled.map((i) => i.key)).toEqual(full.slice(0, 2).map((i) => i.key));
  });
});

describe("feedRowStatus", () => {
  const e = (kind: string, status: Activity["status"], agent_id = "main"): Activity => ({ t: 1, agent_id, kind, text: "x", status });
  const live = liveAgentIds([agent({ id: "s1", status: "running" }), agent({ id: "s2", status: "done" }), agent({ id: "s3", status: "waiting" })]);
  it("renders running tool rows neutral", () => {
    expect(feedRowStatus(e("tool", "running"), live)).toBe("info");
  });
  it("keeps running for agent-start rows only while the lane is live", () => {
    expect(feedRowStatus(e("agent", "running", "s1"), live)).toBe("running");
    expect(feedRowStatus(e("agent", "running", "s3"), live)).toBe("running");
    expect(feedRowStatus(e("agent", "running", "s2"), live)).toBe("info");
    expect(feedRowStatus(e("agent", "running", "gone"), live)).toBe("info");
  });
  it("keeps error, ok and info as they are", () => {
    expect(feedRowStatus(e("tool", "error"), live)).toBe("error");
    expect(feedRowStatus(e("tool", "ok"), live)).toBe("ok");
    expect(feedRowStatus(e("session", "info"), live)).toBe("info");
  });
});
