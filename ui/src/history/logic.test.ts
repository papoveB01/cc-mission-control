import { describe, expect, it } from "vitest";
import { formatTokens } from "../format";
import type { Agent } from "../types";
import { defaultFileView, filterFiles, nextSort, sortFiles, viewFiles } from "./filesTable";
import { formatExact, machineLabel } from "./fmt";
import { dayEnd, dayStart, filtersActive, filtersFromParams, filtersToApi, filtersToParams, noListFilters } from "./listFilters";
import { backOf, buildHash, filesHash, parseHash, sanitizeBack, sessionHash, type Route } from "./route";
import type { FileAccess } from "./types";
import { barSegments, buildUsage, totalTokens } from "./usage";

describe("routes", () => {
  it("parses the three views and unknown hashes", () => {
    expect(parseHash("")).toEqual({ name: "list", filters: noListFilters });
    expect(parseHash("#/")).toEqual({ name: "list", filters: noListFilters });
    expect(parseHash("#/s/m1/s1")).toEqual({ name: "session", machine: "m1", session: "s1", tab: "hud", back: "" });
    expect(parseHash("#/s/m1/s1?tab=usage")).toMatchObject({ tab: "usage" });
    expect(parseHash("#/s/m1/s1?tab=bogus")).toMatchObject({ tab: "hud" });
    expect(parseHash("#/files?path=%2Fa%2Fb.py")).toEqual({ name: "files", path: "/a/b.py", mode: "exact", back: "" });
    expect(parseHash("#/files?prefix=%2Fa%2F")).toEqual({ name: "files", path: "/a/", mode: "prefix", back: "" });
    expect(parseHash("#/files")).toEqual({ name: "files", path: "", mode: "exact", back: "" });
    expect(parseHash("#/nope").name).toBe("notfound");
    expect(parseHash("#/s/only-one").name).toBe("notfound");
    expect(parseHash("#/s/m/s/extra").name).toBe("notfound");
  });

  it("encodes every path segment, including slashes and unicode", () => {
    const h = sessionHash("host/a b", "sess#1?x", "", "files");
    expect(h).toBe("#/s/host%2Fa%20b/sess%231%3Fx?tab=files");
    expect(parseHash(h)).toEqual({ name: "session", machine: "host/a b", session: "sess#1?x", tab: "files", back: "" });
    const u = sessionHash("mé", "日本");
    expect(parseHash(u)).toMatchObject({ machine: "mé", session: "日本" });
  });

  it("round-trips every route", () => {
    const routes: Route[] = [
      { name: "list", filters: noListFilters },
      { name: "list", filters: { ...noListFilters, machine: "a b", q: "x&y=z", file: "/p/q/" } },
      { name: "session", machine: "m/1", session: "s 1", tab: "hud", back: "" },
      { name: "session", machine: "m", session: "s", tab: "usage", back: "/?machine=a&q=b%20c" },
      { name: "files", path: "/w/a b.py", mode: "exact", back: "" },
      { name: "files", path: "/w/dir/", mode: "prefix", back: "" },
      { name: "files", path: "/w/a.py", mode: "exact", back: "/s/m/s1?tab=files&back=%2F" },
    ];
    for (const r of routes) expect(parseHash(buildHash(r))).toEqual(r);
  });

  it("survives malformed percent-encoding", () => {
    expect(parseHash("#/s/%E0%A4%A/x").name).toBe("notfound");
  });

  it("only accepts in-page back targets", () => {
    expect(sanitizeBack("/?q=a")).toBe("/?q=a");
    expect(sanitizeBack("/files?path=x")).toBe("/files?path=x");
    expect(sanitizeBack("https://evil.example/")).toBe("");
    expect(sanitizeBack("//evil.example")).toBe("");
    expect(sanitizeBack("/s/m/s")).toBe("");
    expect(backOf({ name: "list", filters: { ...noListFilters, machine: "m" } })).toBe("/?machine=m");
    expect(filesHash("/a", "prefix")).toBe("#/files?prefix=%2Fa");
    // the lookup may return to the session it came from, a session may not chain to another one
    expect(parseHash(filesHash("/a", "exact", "/s/m/s1?tab=files"))).toMatchObject({ back: "/s/m/s1?tab=files" });
    expect(sanitizeBack("/s/m/s1", true)).toBe("/s/m/s1");
    expect(sanitizeBack("//x", true)).toBe("");
  });
});

describe("list filters and the hash query", () => {
  const f = { machine: "mach-a", project: "proj", q: "parser bug", from: "2026-10-01", to: "2026-10-31", file: "/work/proj/" };

  it("round-trips through the hash", () => {
    const hash = buildHash({ name: "list", filters: f });
    expect(hash.startsWith("#/?")).toBe(true);
    expect(parseHash(hash)).toEqual({ name: "list", filters: f });
  });

  it("omits empty filters and ignores bad dates", () => {
    expect(filtersToParams(noListFilters).toString()).toBe("");
    expect(filtersToParams({ ...noListFilters, q: "  " }).toString()).toBe("");
    const p = filtersFromParams(new URLSearchParams("from=yesterday&to=2026-13-40x&q=a"));
    expect(p.from).toBe("");
    expect(p.to).toBe("");
    expect(p.q).toBe("a");
    expect(filtersActive(noListFilters)).toBe(false);
    expect(filtersActive(f)).toBe(true);
  });

  it("builds API params with epoch seconds for the day range", () => {
    const p = filtersToApi(f, "CUR");
    expect(p.get("machine")).toBe("mach-a");
    expect(p.get("has_file")).toBe("/work/proj/");
    expect(p.get("cursor")).toBe("CUR");
    expect(p.get("limit")).toBe("50");
    expect(Number(p.get("from"))).toBe(dayStart("2026-10-01"));
    expect(Number(p.get("to"))).toBe(dayEnd("2026-10-31"));
    expect((dayEnd("2026-10-31") as number) - (dayStart("2026-10-31") as number)).toBeGreaterThan(23 * 3600);
    expect(filtersToApi(noListFilters, null).has("cursor")).toBe(false);
  });
});

describe("usage totals", () => {
  const agents = [{ id: "main" }, { id: "ag1" }] as Pick<Agent, "id">[];
  const names = new Map([["ag1", "Explore"]]);
  const usage = {
    main: { input: 100, cache_creation: 10, cache_read: 20, output: 30, messages: 1 },
    ag1: { input: 51, cache_creation: 1, cache_read: 6, output: 8, messages: 2 },
    total: { input: 151, cache_creation: 11, cache_read: 26, output: 38, messages: 3 },
  };

  it("builds per-agent rows in lane order and a total row", () => {
    const { rows, total } = buildUsage(usage, agents, names);
    expect(rows.map((r) => [r.id, r.name, r.total])).toEqual([["main", "Main", 160], ["ag1", "Explore", 66]]);
    expect(total).toMatchObject({ id: "total", name: "Total", input: 151, output: 38, messages: 3, total: 226 });
  });

  it("sums the rows when the server sent no total, and includes agents only seen in usage", () => {
    const { total, rows } = buildUsage({ main: usage.main, ghost: usage.ag1 }, agents, names);
    expect(rows.map((r) => r.id)).toEqual(["main", "ghost"]);
    expect(total.total).toBe(226);
  });

  it("formats compact numbers and handles empty and invalid data", () => {
    expect(formatTokens(118_000)).toBe("118k");
    expect(formatTokens(1_200)).toBe("1.2k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
    expect(formatExact(118_000)).toBe("118,000");
    expect(buildUsage(undefined, agents, names).rows).toEqual([]);
    expect(buildUsage({ main: { input: -5, output: Number.NaN } as never }, agents, names).rows[0]?.total).toBe(0);
    expect(totalTokens({ tokens_in: 1, tokens_cache_create: 2, tokens_cache_read: 3, tokens_out: 4 })).toBe(10);
  });

  it("scales bar segments to the busiest agent", () => {
    const { rows } = buildUsage(usage, agents, names);
    const max = 160;
    const segs = barSegments(rows[0]!, max);
    expect(segs.reduce((n, s) => n + s.pct, 0)).toBeCloseTo(100);
    expect(barSegments(rows[1]!, max).reduce((n, s) => n + s.pct, 0)).toBeCloseTo((66 / 160) * 100);
    expect(barSegments(rows[0]!, 0)).toEqual([]);
  });

  it("labels machines", () => {
    expect(machineLabel("alice", "box", "m1")).toBe("alice@box");
    expect(machineLabel("", "box", "m1")).toBe("box");
    expect(machineLabel(null, null, "m1")).toBe("m1");
  });
});

describe("files table defaults", () => {
  it("starts newest first, the same state a click on Time produces", () => {
    expect(defaultFileView).toMatchObject({ sort: "time", dir: "desc" });
    expect(nextSort({ ...defaultFileView, sort: "path" }, "time")).toMatchObject({ sort: "time", dir: "desc" });
  });
});

describe("files table", () => {
  const rows: FileAccess[] = [
    { agent_id: "main", call_id: "c1", path: "/w/b.py", op: "edit", tool: "Edit", status: "ok", t: 105 },
    { agent_id: "ag1", call_id: "c2", path: "/w/A.py", op: "read", tool: "Read", status: "ok", t: 103 },
    { agent_id: "main", call_id: "c3", path: "/w/c.py", op: "write", tool: "Write", status: "error", t: 107 },
    { agent_id: "main", call_id: "c4", path: "/w", op: "search", tool: "Grep", status: "ok", t: 109 },
  ];
  const label = (id: string): string => (id === "main" ? "Main" : "Explore");
  const ids = (r: FileAccess[]): string[] => r.map((x) => x.call_id);

  it("filters by op and by case-insensitive path substring", () => {
    expect(ids(filterFiles(rows, "all", ""))).toEqual(["c1", "c2", "c3", "c4"]);
    expect(ids(filterFiles(rows, "read", ""))).toEqual(["c2"]);
    expect(ids(filterFiles(rows, "all", "a.PY"))).toEqual(["c2"]);
    expect(ids(filterFiles(rows, "write", "b.py"))).toEqual([]);
    expect(ids(filterFiles(rows, "all", "  "))).toHaveLength(4);
  });

  it("sorts by each column in both directions, without mutating the input", () => {
    const copy = [...rows];
    expect(ids(sortFiles(rows, "time", "asc", label))).toEqual(["c2", "c1", "c3", "c4"]);
    expect(ids(sortFiles(rows, "time", "desc", label))).toEqual(["c4", "c3", "c1", "c2"]);
    expect(ids(sortFiles(rows, "path", "asc", label))).toEqual(["c4", "c2", "c1", "c3"]);
    expect(ids(sortFiles(rows, "op", "asc", label))).toEqual(["c1", "c2", "c4", "c3"]);
    expect(ids(sortFiles(rows, "agent", "asc", label))).toEqual(["c2", "c1", "c3", "c4"]);
    expect(ids(sortFiles(rows, "status", "desc", label))).toEqual(["c2", "c1", "c4", "c3"]);
    expect(rows).toEqual(copy);
  });

  it("combines filter and sort, and toggles direction on the active column", () => {
    const v = { ...defaultFileView, op: "all" as const, q: "/w/", sort: "path" as const, dir: "desc" as const };
    expect(ids(viewFiles(rows, v, label))).toEqual(["c3", "c1", "c2"]);
    expect(nextSort(defaultFileView, "time")).toMatchObject({ sort: "time", dir: "asc" }); // already newest first: flips
    expect(nextSort({ ...defaultFileView, sort: "path", dir: "asc" }, "path")).toMatchObject({ dir: "desc" });
    expect(nextSort(defaultFileView, "path")).toMatchObject({ sort: "path", dir: "asc" });
  });
});
