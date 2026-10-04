import { describe, expect, it } from "vitest";
import { buildResults, flatten } from "./palette";
import { DOC_TITLE_NAME_MAX, narrowName, parseMessage, sessionName, tabLabels, tabTooltip, TAB_NAME_MAX, truncateName } from "./store";
import type { Session } from "./types";

const s = (id: string, over: Partial<Session> = {}): Session => ({
  id, title: "proj", cwd: "/work/proj", model: null, status: "active", started: 100, ended: null, last_event: 100,
  compactions: 0, agents: [], activity: [], ...over,
});

describe("session name fallback", () => {
  it("uses name, else the project folder (older servers send no name)", () => {
    expect(sessionName(s("a", { name: "Subscription plan model design" }))).toBe("Subscription plan model design");
    expect(sessionName(s("a"))).toBe("proj");
    expect(sessionName(s("a", { name: "   " }))).toBe("proj");
  });

  it("narrows the optional fields at the JSON boundary", () => {
    const raw = { ...s("a"), name: 42, name_source: "weird" } as unknown as Session;
    const n = narrowName(raw);
    expect("name" in n).toBe(false);
    expect("name_source" in n).toBe(false);
    const ok = narrowName({ ...s("a"), name: "Hi", name_source: "custom" });
    expect(ok).toMatchObject({ name: "Hi", name_source: "custom" });
  });

  it("parses messages from old and new servers", () => {
    const frame = (sessions: unknown[]) => JSON.stringify({ type: "snapshot", version: "x", sessions });
    const old = parseMessage(frame([s("a")]));
    expect(old?.sessions[0] && "name" in old.sessions[0]).toBe(false);
    const fresh = parseMessage(frame([{ ...s("b"), name: "Named", name_source: "generated" }]));
    expect(fresh?.sessions[0]).toMatchObject({ name: "Named", name_source: "generated", title: "proj" });
  });
});

describe("tab labels", () => {
  it("shows the name and falls back to the title", () => {
    const labels = tabLabels([s("a", { name: "My session" }), s("b", { title: "other" })]);
    expect(labels.get("a")).toBe("My session");
    expect(labels.get("b")).toBe("other");
  });

  it("truncates long names with an ellipsis around 28 characters", () => {
    const long = "Subscription plan model design and rollout";
    const label = tabLabels([s("a", { name: long })]).get("a") as string;
    expect(label.endsWith("\u2026")).toBe(true);
    expect([...label].length).toBeLessThanOrEqual(TAB_NAME_MAX);
    expect(truncateName("short")).toBe("short");
    expect(truncateName("x".repeat(TAB_NAME_MAX))).toBe("x".repeat(TAB_NAME_MAX));
  });

  it("appends the start time when two tabs would show the same text, and for ended tabs", () => {
    const labels = tabLabels([
      s("a", { name: "Same name" }),
      s("b", { name: "Same name", started: 100 + 3600 }),
      s("c", { name: "Unique" }),
      s("d", { name: "Old one", status: "ended" }),
    ]);
    expect(labels.get("a")).toMatch(/^Same name \u00b7 \d\d:\d\d$/);
    expect(labels.get("b")).toMatch(/^Same name \u00b7 \d\d:\d\d$/);
    expect(labels.get("c")).toBe("Unique");
    expect(labels.get("d")).toMatch(/^Old one \u00b7 \d\d:\d\d$/);
  });

  it("treats names that only differ after the truncation point as duplicates", () => {
    const base = "a".repeat(40);
    const labels = tabLabels([s("a", { name: base + "1" }), s("b", { name: base + "2", started: 100 + 120 })]);
    expect(labels.get("a")).toMatch(/\u00b7 \d\d:\d\d$/);
  });

  it("uses seconds when two tabs still collide after HH:MM", () => {
    // Same name, same minute (100 s and 105 s are both 00:01 UTC-ish; only the seconds differ).
    const base = new Date(2026, 0, 2, 3, 4, 10).getTime() / 1000;
    const labels = tabLabels([
      s("a", { name: "Twin", started: base }),
      s("b", { name: "Twin", started: base + 7 }),
      s("c", { name: "Other", started: base }),
    ]);
    expect(labels.get("a")).toBe("Twin \u00b7 03:04:10");
    expect(labels.get("b")).toBe("Twin \u00b7 03:04:17");
    expect(labels.get("c")).toBe("Other");
  });

  it("keeps HH:MM when the minutes differ", () => {
    const base = new Date(2026, 0, 2, 3, 4, 10).getTime() / 1000;
    const labels = tabLabels([s("a", { name: "Twin", started: base }), s("b", { name: "Twin", started: base + 120 })]);
    expect(labels.get("a")).toBe("Twin \u00b7 03:04");
    expect(labels.get("b")).toBe("Twin \u00b7 03:06");
  });

  it("truncates the browser-tab name to 60 characters", () => {
    const t = truncateName("n".repeat(80), DOC_TITLE_NAME_MAX);
    expect([...t].length).toBe(DOC_TITLE_NAME_MAX);
    expect(t.endsWith("\u2026")).toBe(true);
    expect(truncateName("short", DOC_TITLE_NAME_MAX)).toBe("short");
  });

  it("different sessions in one project keep distinct names without a time", () => {
    const labels = tabLabels([s("a", { name: "Plan A" }), s("b", { name: "Plan B" })]);
    expect([labels.get("a"), labels.get("b")]).toEqual(["Plan A", "Plan B"]);
  });
});

describe("tab tooltip", () => {
  it("shows name, project, start time and the name source", () => {
    const t = tabTooltip(s("a", { name: "Full name here", name_source: "custom" }));
    expect(t).toContain("Full name here");
    expect(t).toContain("Project: /work/proj");
    expect(t).toMatch(/Started \d\d:\d\d/);
    expect(t).toContain("custom name");
  });
  it("marks generated names as auto, only in the tooltip", () => {
    expect(tabTooltip(s("a", { name: "Gen", name_source: "generated" }))).toContain("auto");
    expect(tabLabels([s("a", { name: "Gen", name_source: "generated" })]).get("a")).toBe("Gen");
    expect(tabTooltip(s("a"))).toContain("folder name");
  });
});

describe("palette session matching", () => {
  const sessions = [
    s("a", { name: "Subscription plan model design", title: "billing-app", name_source: "generated" }),
    s("b", { name: "Fix login redirect", title: "web-frontend", name_source: "custom" }),
  ];
  const ctx = { sessions, selectedId: "a", names: new Map<string, string>() };
  const hits = (q: string) => flatten(buildResults(q, ctx)).filter((i) => i.target.type === "session");

  it("matches on the session name", () => {
    expect(hits("subscription").map((i) => i.id)).toEqual(["session:a"]);
    expect(hits("redirect").map((i) => i.id)).toEqual(["session:b"]);
  });
  it("matches on the project folder", () => {
    expect(hits("billing").map((i) => i.id)).toEqual(["session:a"]);
    expect(hits("web-front").map((i) => i.id)).toEqual(["session:b"]);
  });
  it("shows the name with the project as secondary text", () => {
    const item = hits("subscription")[0];
    expect(item?.label).toBe("Subscription plan model design");
    expect(item?.detail).toContain("billing-app");
  });
  it("still works with an older server (no name)", () => {
    const old = { sessions: [s("z", { title: "legacy" })], selectedId: "z", names: new Map<string, string>() };
    expect(flatten(buildResults("legacy", old)).some((i) => i.id === "session:z")).toBe(true);
  });
});
