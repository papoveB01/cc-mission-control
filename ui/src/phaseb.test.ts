import { describe, expect, it } from "vitest";
import { clampDock, DOCK_DEFAULT, DOCK_KEY, loadDock, parseDock, saveDock } from "./dock";
import {
  entryTool, pruneFilters, FILTER_KEY, filtersActive, loadFilters, matchCall, matchEntry, noFilters, parseFilters, saveFilters, toggleIn, type Filters,
} from "./filters";
import { fuzzyScore, rank } from "./fuzzy";
import { buildResults, flatten, recentCalls } from "./palette";
import { dispatchKey, type KeyInfo } from "./shortcuts";
import { initialState, PENDING_MAX_SAMPLES, reduce, historyKey, type ServerMessage } from "./store";
import {
  altRows, barLayout, clampStart, errorCalls, fitsLabel, followEdge, followView, initialTimeline, MAX_SPAN, MIN_SPAN, nearestBar, nextError, niceTicks,
  sessionExtent, timelineReducer, xOf, FOLLOW_ANCHOR, DEFAULT_SPAN, type TimelineState,
} from "./timeline";
import type { Activity, Agent, Session, ToolCall } from "./types";

const call = (id: string, over: Partial<ToolCall> = {}): ToolCall => ({
  id, agent_id: "main", tool: "Bash", summary: "pytest -q", status: "ok", started: 100, ended: 110, duration_ms: 10_000, subagent_id: null, ...over,
});
const agent = (over: Partial<Agent> = {}): Agent => ({
  id: "main", label: "Main", agent_type: null, status: "running", task: "", result: "", started: 1, ended: null, context_tokens: 1000,
  context_window: 200000, model: null, tool_counts: {}, errors: 0, total_calls: 0, calls: [], ...over,
});
const session = (agents: Agent[], over: Partial<Session> = {}): Session => ({
  id: "s", title: "proj", cwd: "/p", model: null, status: "active", started: 50, ended: null, last_event: 120, compactions: 0, agents, activity: [], ...over,
});

describe("timeline scale", () => {
  const view = { start: 100, span: 50 };
  it("maps time to pixels and back to the same ratio", () => {
    expect(xOf(100, view, 500)).toBe(0);
    expect(xOf(125, view, 500)).toBe(250);
    expect(xOf(150, view, 500)).toBe(500);
  });
  it("lays out finished and running bars", () => {
    const done = barLayout(call("a"), view, 500, 140);
    expect(done).toMatchObject({ x: 0, w: 100, running: false, visible: true });
    const run = barLayout(call("b", { status: "running", ended: null, duration_ms: null, started: 130 }), view, 500, 140);
    expect(run.running).toBe(true);
    expect(run.x).toBe(300);
    expect(run.w).toBe(100); // extends to now (140)
    const later = barLayout(call("b", { status: "running", ended: null, duration_ms: null, started: 130 }), view, 500, 145);
    expect(later.w).toBe(150); // the moving edge
  });
  it("gives instant calls a minimum width and flags offscreen bars", () => {
    expect(barLayout(call("c", { started: 120, ended: 120 }), view, 500, 0).w).toBeGreaterThanOrEqual(3);
    expect(barLayout(call("d", { started: 10, ended: 20 }), view, 500, 0).visible).toBe(false);
    expect(barLayout(call("e", { started: 400, ended: 410 }), view, 500, 0).visible).toBe(false);
  });
  it("only labels bars wide enough", () => {
    expect(fitsLabel(30, "Bash")).toBe(false);
    expect(fitsLabel(60, "Bash")).toBe(true);
  });
});

describe("timeline ticks", () => {
  const utc = (): number => 0;
  it("picks nice steps at least minPx apart, aligned to the local clock", () => {
    const ticks = niceTicks({ start: 1000, span: 100 }, 1000, 96, utc);
    const steps = ticks.slice(1).map((t, i) => t.t - (ticks[i]?.t ?? 0));
    expect(new Set(steps).size).toBe(1);
    expect(steps[0]).toBeGreaterThanOrEqual((100 * 96) / 1000);
    expect([1, 2, 5, 10, 15, 30, 60]).toContain(steps[0]);
    expect(ticks.every((t) => t.t % (steps[0] as number) === 0)).toBe(true);
  });
  it("coarsens when zoomed out and refines when zoomed in", () => {
    const wide = niceTicks({ start: 0, span: 86_400 }, 1000, 96, utc);
    const tight = niceTicks({ start: 0, span: 10 }, 1000, 96, utc);
    expect((wide[1]?.t ?? 0) - (wide[0]?.t ?? 0)).toBeGreaterThan(600);
    expect((tight[1]?.t ?? 0) - (tight[0]?.t ?? 0)).toBeLessThanOrEqual(2);
  });
  it("shows dates at day-scale steps", () => {
    const ticks = niceTicks({ start: 0, span: 86_400 * 20 }, 1000, 96, utc);
    expect(ticks.length).toBeGreaterThan(1);
    expect(ticks[0]?.label).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/);
  });
  it("applies the UTC offset per tick (a DST change mid-window)", () => {
    // Offset jumps from +0 to +3600 at t = 5000.
    const offsetAt = (t: number): number => (t < 5000 ? 0 : 3600);
    const ticks = niceTicks({ start: 0, span: 10_000 }, 1000, 96, offsetAt);
    for (const t of ticks) expect((t.t + offsetAt(t.t)) % 900).toBe(0);
  });
  it("returns nothing for degenerate input", () => {
    expect(niceTicks({ start: 0, span: 10 }, 0)).toEqual([]);
  });
});

describe("timeline reducer", () => {
  const view = { start: 1000, span: 100 };
  const base: TimelineState = { start: 1000, span: 100, follow: false, auto: false };
  it("zoom keeps the anchor time fixed", () => {
    const z = timelineReducer(base, { type: "zoom", factor: 2, anchor: 0.25, view });
    expect(z.span).toBe(50);
    expect(z.start + 0.25 * z.span).toBeCloseTo(view.start + 0.25 * view.span);
  });
  it("clamps the span", () => {
    expect(timelineReducer(base, { type: "zoom", factor: 1e9, anchor: 0.5, view }).span).toBe(MIN_SPAN);
    expect(timelineReducer(base, { type: "zoom", factor: 1e-9, anchor: 0.5, view }).span).toBe(MAX_SPAN);
  });
  it("panning moves the window from the shown view and turns follow off", () => {
    const p = timelineReducer({ ...base, follow: true, auto: true }, { type: "pan", dt: 30, view: { start: 5000, span: 40 } });
    expect(p.start).toBe(5030);
    expect(p.span).toBe(40);
    expect(p.follow).toBe(false);
    expect(p.auto).toBe(false);
  });
  it("clamps panning to [t0 - span/2, edge + span/2]", () => {
    const bounds = { t0: 1000, t1: 2000 };
    expect(clampStart(0, 100, bounds)).toBe(950);
    expect(clampStart(9999, 100, bounds)).toBe(2050);
    expect(clampStart(1500, 100, bounds)).toBe(1500);
    expect(timelineReducer(base, { type: "pan", dt: -1e6, view, bounds }).start).toBe(950);
  });
  it("zoom while following keeps following and disables auto zoom", () => {
    const f = timelineReducer({ start: 0, span: 120, follow: true, auto: true }, { type: "zoom", factor: 2, anchor: 0, view: { start: 0, span: 60 } });
    expect(f.follow).toBe(true);
    expect(f.auto).toBe(false);
    expect(f.span).toBe(30);
  });
  it("fit shows the whole range with padding; follows only if the fit includes now", () => {
    const f = timelineReducer(initialTimeline, { type: "fit", t0: 100, t1: 300, live: false });
    expect(f.follow).toBe(false);
    expect(f.start).toBeLessThan(100);
    expect(f.start + f.span).toBeGreaterThan(300);
    expect(timelineReducer(initialTimeline, { type: "fit", t0: 100, t1: 300, live: true }).follow).toBe(true);
  });
  it("turning follow off freezes the derived view; turning it on resumes", () => {
    const off = timelineReducer(initialTimeline, { type: "follow", on: false, view: { start: 7, span: 33 } });
    expect(off).toMatchObject({ start: 7, span: 33, follow: false, auto: false });
    expect(timelineReducer(off, { type: "follow", on: true }).follow).toBe(true);
  });
  it("centres on a time without following, and reset restores the defaults", () => {
    const c = timelineReducer({ ...base, follow: true }, { type: "center", t: 1234, view });
    expect(c.start + c.span / 2).toBeCloseTo(1234);
    expect(c.follow).toBe(false);
    expect(timelineReducer(c, { type: "reset" })).toEqual(initialTimeline);
  });
});

describe("follow-live window", () => {
  it("shows the whole session while it is younger than the span, then slides", () => {
    const young = followView({ span: DEFAULT_SPAN, auto: true }, 1000, 1030);
    expect(young.start).toBeLessThan(1000);
    expect(young.start + young.span).toBeGreaterThan(1030);
    expect(young.span).toBeLessThan(DEFAULT_SPAN);
    const old = followView({ span: DEFAULT_SPAN, auto: true }, 1000, 1500);
    expect(old.span).toBe(DEFAULT_SPAN);
    expect(old.start + old.span * FOLLOW_ANCHOR).toBeCloseTo(1500);
  });
  it("uses the chosen span once auto zoom is off", () => {
    const v = followView({ span: 20, auto: false }, 1000, 1005);
    expect(v.span).toBe(20);
    expect(v.start + 20 * FOLLOW_ANCHOR).toBeCloseTo(1005);
  });
  it("never goes below the minimum span", () => {
    expect(followView({ span: DEFAULT_SPAN, auto: true }, 1000, 1000.1).span).toBeGreaterThanOrEqual(MIN_SPAN);
  });
});

describe("timeline extent and navigation", () => {
  const a = agent({ calls: [call("1", { started: 100, ended: 105 }), call("2", { started: 130, status: "running", ended: null, duration_ms: null })] });
  it("tracks the live edge only while something runs", () => {
    const live = sessionExtent(session([a]), 200);
    expect(live).toMatchObject({ t0: 100, t1: 200, running: true });
    expect(followEdge(live, 200)).toBe(200);
    const idle = sessionExtent(session([agent({ calls: [call("1", { started: 100, ended: 105 })] })]), 200);
    expect(followEdge(idle, 200)).toBe(105);
  });
  it("falls back to the session range with no calls", () => {
    expect(sessionExtent(session([agent()]), 0).t0).toBe(50);
  });
  it("finds the nearest bar and the next error with wraparound", () => {
    const bars = [call("1", { started: 10 }), call("2", { started: 50 }), call("3", { started: 90 })];
    expect(nearestBar(bars, 60)?.id).toBe("2");
    expect(nearestBar([], 1)).toBeNull();
    const errs = errorCalls(session([agent({ calls: [call("y", { status: "error", started: 9 }), call("x", { status: "error", started: 5 }), call("z")] })]));
    expect(errs.map((c) => c.id)).toEqual(["x", "y"]);
    expect(nextError(errs, null)?.id).toBe("x");
    expect(nextError(errs, "x")?.id).toBe("y");
    expect(nextError(errs, "y")?.id).toBe("x");
    expect(nextError([], null)).toBeNull();
  });
});

describe("error ordering and text alternative", () => {
  it("sorts errors by start, then agent order, then id", () => {
    const a = agent({ id: "main", calls: [call("b", { status: "error", started: 5, agent_id: "main" })] });
    const b = agent({ id: "sub", calls: [call("a", { status: "error", started: 5, agent_id: "sub" }), call("c", { status: "error", started: 5, agent_id: "sub" })] });
    expect(errorCalls(session([a, b])).map((c) => c.id)).toEqual(["b", "a", "c"]);
  });
  it("caps the text alternative to the newest calls and reports the total", () => {
    const calls = Array.from({ length: 300 }, (_, i) => call(`c${i}`, { started: i }));
    const { rows, total } = altRows([{ id: "main", calls }]);
    expect(rows).toHaveLength(200);
    expect(total).toBe(300);
    expect(rows[0]?.call.id).toBe("c299");
  });
});

describe("fuzzy matcher", () => {
  it("requires an in-order subsequence", () => {
    expect(fuzzyScore("gp", "general-purpose")).not.toBeNull();
    expect(fuzzyScore("pg", "general-purpose")).toBeNull();
    expect(fuzzyScore("", "anything")).toBe(0);
  });
  it("ranks prefix and word-start matches above scattered ones", () => {
    const items = ["code-reviewer", "Explore", "general-purpose 1", "xcxoxdxe"];
    expect(rank(items, "code", (s) => s)[0]).toBe("code-reviewer");
    expect(rank(items, "gp", (s) => s)[0]).toBe("general-purpose 1");
    expect(rank(items, "xyz", (s) => s)).toEqual([]);
  });
  it("rejects scattered letters across long paths (the reported 'rev' case)", () => {
    expect(fuzzyScore("rev", "Read /Users/dev/work/x/rules.py")).toBeNull();
    expect(fuzzyScore("rev", "Read /Users/dev/work/payments-api/app/db/idempotency.py")).toBeNull();
    expect(fuzzyScore("rev", "code-reviewer 1")).not.toBeNull();
    // word-start runs ("re" + "v") must not match by chance inside a long call line
    expect(fuzzyScore("rev", "Read /Users/dev/work/fraud-scoring/src/fraud/velocity.py")).toBeNull();
    expect(fuzzyScore("gp", "Read /Users/dev/work/fraud-scoring/src/general-purpose.py")).toBeNull();
  });
  it("prefers contiguous substrings and word starts over acronyms", () => {
    const contiguous = fuzzyScore("rev", "code-reviewer") ?? 0;
    const acronym = fuzzyScore("cr", "code-reviewer") ?? 0;
    expect(contiguous).toBeGreaterThan(acronym);
    expect(fuzzyScore("rev", "reviewer")).toBeGreaterThan(fuzzyScore("rev", "code-reviewer") ?? 0);
    expect(fuzzyScore("gpurp", "general-purpose")).not.toBeNull();
  });
  it("is case-insensitive and keeps order for an empty query", () => {
    expect(rank(["B", "a"], "", (s) => s)).toEqual(["B", "a"]);
    expect(rank(["Explore"], "EXP", (s) => s)).toEqual(["Explore"]);
  });
});

describe("palette results", () => {
  const sub = agent({ id: "s1", label: "Explore", calls: [call("c9", { agent_id: "s1", tool: "Grep", summary: "score_txn", started: 300 })] });
  const main = agent({ calls: [call("c1", { started: 100 }), call("c2", { started: 200, tool: "Edit", summary: "src/fraud/score.py" })] });
  const sess = session([main, sub]);
  const ctx = { sessions: [sess], selectedId: "s", names: new Map([["main", "Main"], ["s1", "Explore"]]) };
  it("groups and limits an empty query", () => {
    const groups = buildResults("", ctx);
    expect(groups.map((g) => g.heading)).toEqual(["Actions", "Agents", "Sessions", "Recent calls"]);
    expect(groups.find((g) => g.heading === "Recent calls")?.items.length).toBeLessThanOrEqual(3);
  });
  it("finds agents, calls and actions by fuzzy query", () => {
    expect(flatten(buildResults("expl", ctx))[0]?.target).toEqual({ type: "agent", id: "s1" });
    const call = flatten(buildResults("score", ctx)).find((i) => i.target.type === "call");
    expect(call).toBeDefined();
    expect(flatten(buildResults("fit", ctx))[0]?.target).toEqual({ type: "action", id: "fit" });
  });
  it("ranks the matching agent first for 'rev' and keeps scattered path calls out", () => {
    const rev = agent({ id: "r1", label: "code-reviewer" });
    const mainWith = agent({ calls: [call("p1", { tool: "Read", summary: "/Users/dev/work/x/rules.py", started: 400 })] });
    const c = { sessions: [session([mainWith, rev])], selectedId: "s", names: new Map([["main", "Main"], ["r1", "code-reviewer 1"]]) };
    const groups = buildResults("rev", c);
    expect(groups[0]?.heading).toBe("Agents");
    expect(groups[0]?.items[0]?.target).toEqual({ type: "agent", id: "r1" });
    expect(groups.some((g) => g.heading === "Recent calls")).toBe(false);
  });
  it("orders groups by their best score, so actions are not always first", () => {
    const groups = buildResults("expl", ctx);
    expect(groups[0]?.heading).toBe("Agents");
    expect(buildResults("fit", ctx)[0]?.heading).toBe("Actions");
  });
  it("drops empty groups and orders recent calls newest first", () => {
    expect(buildResults("zzzzqq", ctx)).toEqual([]);
    expect(recentCalls(sess).map((c) => c.id)).toEqual(["c9", "c2", "c1"]);
    expect(recentCalls(null)).toEqual([]);
  });
});

describe("shortcut dispatcher", () => {
  const k = (key: string, over: Partial<KeyInfo> = {}): KeyInfo => ({ key, ctrl: false, meta: false, alt: false, typing: false, ...over });
  it("maps the documented keys", () => {
    expect(dispatchKey(k("?"))).toBe("help");
    expect(dispatchKey(k("["))).toBe("prev-session");
    expect(dispatchKey(k("]"))).toBe("next-session");
    expect(dispatchKey(k("g"))).toBe("topology");
    expect(dispatchKey(k("t"))).toBe("timeline");
    expect(dispatchKey(k("f"))).toBe("feed-filter");
    expect(dispatchKey(k("e"))).toBe("next-error");
    expect(dispatchKey(k("Escape"))).toBe("escape");
    expect(dispatchKey(k("x"))).toBeNull();
  });
  it("ignores shortcuts while typing", () => {
    for (const key of ["?", "[", "g", "t", "f", "e", "Escape"]) expect(dispatchKey(k(key, { typing: true }))).toBeNull();
  });
  it("ignores key repeat and IME composition, and is case-insensitive for letters", () => {
    expect(dispatchKey(k("e", { repeat: true }))).toBeNull();
    expect(dispatchKey(k("e", { composing: true }))).toBeNull();
    expect(dispatchKey(k("k", { ctrl: true, repeat: true }))).toBeNull();
    expect(dispatchKey(k("E"))).toBe("next-error");
    expect(dispatchKey(k("G"))).toBe("topology");
  });
  it("ignores shortcuts with Cmd, Ctrl or Alt held, except the palette", () => {
    expect(dispatchKey(k("g", { ctrl: true }))).toBeNull();
    expect(dispatchKey(k("t", { meta: true }))).toBeNull();
    expect(dispatchKey(k("e", { alt: true }))).toBeNull();
    expect(dispatchKey(k("k", { ctrl: true }))).toBe("palette");
    expect(dispatchKey(k("K", { meta: true }))).toBe("palette");
    expect(dispatchKey(k("k", { ctrl: true, typing: true }))).toBe("palette");
    expect(dispatchKey(k("k", { ctrl: true, alt: true }))).toBeNull();
    expect(dispatchKey(k("k"))).toBeNull();
  });
});

describe("feed filters", () => {
  const f = (over: Partial<Filters> = {}): Filters => ({ ...noFilters, ...over });
  const c = call("1", { agent_id: "a", tool: "Bash", summary: "make build", status: "error" });
  it("matches calls by status, agent, tool and text", () => {
    expect(matchCall(noFilters, c)).toBe(true);
    expect(matchCall(f({ status: "errors" }), c)).toBe(true);
    expect(matchCall(f({ status: "running" }), c)).toBe(false);
    expect(matchCall(f({ agents: ["b"] }), c)).toBe(false);
    expect(matchCall(f({ agents: ["a", "b"] }), c)).toBe(true);
    expect(matchCall(f({ tools: ["Read"] }), c)).toBe(false);
    expect(matchCall(f({ text: "BUILD" }), c)).toBe(true);
    expect(matchCall(f({ text: "nope" }), c)).toBe(false);
    expect(matchCall(f({ status: "errors", agents: ["a"], tools: ["Bash"], text: "make" }), c)).toBe(true);
  });
  const entry = (over: Partial<Activity> = {}): Activity => ({ t: 1, agent_id: "a", kind: "tool", text: "Bash: pytest", status: "info", ...over });
  const live = new Set(["a"]);
  it("filters feed entries", () => {
    expect(entryTool(entry())).toBe("Bash");
    expect(entryTool(entry({ kind: "prompt" }))).toBeNull();
    expect(matchEntry(f({ tools: ["Bash"] }), entry(), live)).toBe(true);
    expect(matchEntry(f({ tools: ["Bash"] }), entry({ kind: "prompt", text: "hello" }), live)).toBe(false);
    expect(matchEntry(f({ status: "errors" }), entry({ status: "error" }), live)).toBe(true);
    expect(matchEntry(f({ status: "errors" }), entry(), live)).toBe(false);
    expect(matchEntry(f({ status: "running" }), entry({ kind: "agent", status: "running" }), live)).toBe(true);
    expect(matchEntry(f({ status: "running" }), entry({ kind: "tool", status: "running" }), live)).toBe(false);
    expect(matchEntry(f({ text: "pytest" }), entry(), live)).toBe(true);
  });
  it("knows when filters are active and toggles list members", () => {
    expect(filtersActive(noFilters)).toBe(false);
    expect(filtersActive(f({ text: "  " }))).toBe(false);
    expect(filtersActive(f({ tools: ["x"] }))).toBe(true);
    expect(toggleIn(["a"], "b")).toEqual(["a", "b"]);
    expect(toggleIn(["a", "b"], "a")).toEqual(["b"]);
  });
  it("prunes stored ids that do not exist in this session", () => {
    const stored = f({ agents: ["a", "ghost"], tools: ["Bash", "Nope"], status: "errors" });
    const { filters, pruned } = pruneFilters(stored, new Set(["a", "b"]), new Set(["Bash"]));
    expect(filters).toEqual({ ...stored, agents: ["a"], tools: ["Bash"] });
    expect(pruned).toBe(2);
    expect(pruneFilters(stored, new Set(["a", "ghost"]), new Set(["Bash", "Nope"]))).toEqual({ filters: stored, pruned: 0 });
  });
  it("persists to storage and survives corrupt or unavailable storage", () => {
    const mem = new Map<string, string>();
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) };
    const want = f({ status: "errors", agents: ["a"], tools: ["Bash"], text: "x" });
    saveFilters(want, store);
    expect(mem.has(FILTER_KEY)).toBe(true);
    expect(loadFilters(store)).toEqual(want);
    mem.set(FILTER_KEY, "{not json");
    expect(loadFilters(store)).toEqual(noFilters);
    mem.set(FILTER_KEY, JSON.stringify({ status: "weird", agents: [1, "ok"], tools: "no", text: 5 }));
    expect(loadFilters(store)).toEqual({ status: "all", agents: ["ok"], tools: [], text: "" });
    const throwing = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
    expect(loadFilters(throwing)).toEqual(noFilters);
    expect(() => saveFilters(want, throwing)).not.toThrow();
    expect(loadFilters(null)).toEqual(noFilters);
    expect(parseFilters(null)).toEqual(noFilters);
  });
});

describe("dock state", () => {
  it("clamps between 120 px and half the viewport", () => {
    expect(clampDock(50, 1000)).toBe(120);
    expect(clampDock(900, 1000)).toBe(500);
    expect(clampDock(300, 1000)).toBe(300);
    expect(clampDock(300, 100)).toBe(120);
  });
  it("persists height and collapsed state, tolerating bad data", () => {
    const mem = new Map<string, string>();
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) };
    saveDock({ height: 260, collapsed: true }, store);
    expect(mem.has(DOCK_KEY)).toBe(true);
    expect(loadDock(store)).toEqual({ height: 260, collapsed: true });
    mem.set(DOCK_KEY, "garbage");
    expect(loadDock(store).height).toBe(DOCK_DEFAULT);
    expect(parseDock('{"height":"x","collapsed":1}')).toEqual({ height: DOCK_DEFAULT, collapsed: false });
    expect(() => saveDock({ height: 1, collapsed: false }, { getItem: () => null, setItem: () => { throw new Error("full"); } })).not.toThrow();
  });
});

describe("history store edge cases", () => {
  const snap = (s: Session[]): ServerMessage => ({ type: "snapshot", version: "x", sessions: s });
  const upd = (s: Session[]): ServerMessage => ({ type: "sessions", sessions: s });
  it("expires a pending compaction after a few non-falling samples", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 1000 })])]), 1);
    st = reduce(st, upd([session([agent({ context_tokens: 1000 })], { compactions: 1 })]), 2);
    expect(st.pending.has("s")).toBe(true);
    for (let i = 0; i < PENDING_MAX_SAMPLES; i++) st = reduce(st, upd([session([agent({ context_tokens: 2000 + i })], { compactions: 1 })]), 3 + i);
    expect(st.pending.has("s")).toBe(false);
    st = reduce(st, upd([session([agent({ context_tokens: 10 })], { compactions: 1 })]), 20);
    expect(st.history.get(historyKey("s", "main"))?.at(-1)?.drop).toBe(false);
  });
  it("expires a pending compaction after 60 s", () => {
    let st = reduce(initialState, snap([session([agent({ context_tokens: 1000 })])]), 0);
    st = reduce(st, upd([session([agent({ context_tokens: 1000 })], { compactions: 1 })]), 1000);
    st = reduce(st, upd([session([agent({ context_tokens: 1500 })], { compactions: 1 })]), 70_000);
    expect(st.pending.has("s")).toBe(false);
  });
  it("snapshot prunes history of agents that vanished from a surviving session", () => {
    let st = reduce(initialState, snap([session([agent(), agent({ id: "x", context_tokens: 5 })])]), 1);
    expect(st.history.has(historyKey("s", "x"))).toBe(true);
    st = reduce(st, snap([session([agent()])]), 2);
    expect(st.history.has(historyKey("s", "x"))).toBe(false);
    expect(st.history.has(historyKey("s", "main"))).toBe(true);
  });
});
