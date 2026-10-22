// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FEED_VIEW_KEY, loadFeedView, matchCall, matchEntry, noFilters, saveFeedView, type FeedView } from "../filters";
import type { Activity, ToolCall } from "../types";
import { ActivityFeed } from "./ActivityFeed";

const entry = (over: Partial<Activity> = {}): Activity => ({
  t: 1000, agent_id: "main", kind: "tool", text: "Bash: uv run --extra dev pytest -q", status: "running",
  plain: "Ran the test suite", call_id: "c1", ...over,
});
const call = (over: Partial<ToolCall> = {}): ToolCall => ({
  id: "c1", agent_id: "main", tool: "Bash", summary: "pytest", status: "ok", started: 1, ended: 3, duration_ms: 2500,
  subagent_id: null, plain: "Ran the test suite", outcome: "489 passed, 1 skipped", ...over,
});

function Harness({ activity, calls }: { activity: Activity[]; calls?: Map<string, ToolCall> }) {
  const [view, setView] = useState<FeedView>(() => loadFeedView());
  const change = (v: FeedView): void => {
    setView(v);
    saveFeedView(v);
  };
  return (
    <ActivityFeed activity={activity} names={new Map([["main", "Main"]])} liveAgents={new Set()} onOpenAgent={() => {}} filters={{ ...noFilters, view }} calls={calls} view={view} onViewChange={change} />
  );
}

function mount(activity: Activity[], calls?: Map<string, ToolCall>) {
  return render(<Harness activity={activity} calls={calls} />);
}

beforeEach(() => {
  (window as unknown as { matchMedia: unknown }).matchMedia = () => ({ matches: false });
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ActivityFeed plain / technical", () => {
  it("renders the plain sentence with outcome and duration, technical text in the tooltip", () => {
    const { container } = mount([entry()], new Map([["c1", call()]]));
    const text = container.querySelector(".feed-text") as HTMLElement;
    expect(text.textContent).toContain("Ran the test suite");
    expect(text.textContent).toContain("489 passed, 1 skipped");
    expect(text.textContent).toContain("2.5s");
    expect(text.getAttribute("title")).toBe("Bash: uv run --extra dev pytest -q");
    expect(container.querySelector(".feed-item")?.className).toContain("st-ok");
  });

  it("omits the duration at 1 s or less and falls back without a joined call", () => {
    const { container } = mount([entry()], new Map([["c1", call({ duration_ms: 1000 })]]));
    expect(container.querySelector(".feed-text")?.textContent).not.toMatch(/\d\.\ds/);
    cleanup();
    const again = mount([entry()]);
    expect(again.container.querySelector(".feed-text")?.textContent).toBe("Ran the test suite");
    expect(again.container.querySelector(".feed-item")?.className).toContain("st-info");
  });

  it("falls back to text for older servers without plain", () => {
    const { container } = mount([entry({ plain: undefined, call_id: undefined })]);
    expect(container.querySelector(".feed-text")?.textContent).toBe("Bash: uv run --extra dev pytest -q");
  });

  it("technical mode shows today's text exactly, with no outcome", () => {
    const { container } = mount([entry()], new Map([["c1", call()]]));
    fireEvent.click(screen.getByRole("button", { name: "Technical" }));
    const text = container.querySelector(".feed-text") as HTMLElement;
    expect(text.textContent).toBe("Bash: uv run --extra dev pytest -q");
    expect(text.hasAttribute("title")).toBe(false);
    expect(container.querySelector(".feed-item")?.className).toContain("st-info");
    expect(screen.getByRole("button", { name: "Technical" }).getAttribute("aria-pressed")).toBe("true");
    expect(localStorage.getItem(FEED_VIEW_KEY)).toBe("technical");
  });

  it("restores the saved mode, and survives storage that throws", () => {
    localStorage.setItem(FEED_VIEW_KEY, "technical");
    const { container } = mount([entry()]);
    expect(container.querySelector(".feed-text")?.textContent).toBe("Bash: uv run --extra dev pytest -q");
    cleanup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const blocked = mount([entry()]);
    expect(blocked.container.querySelector(".feed-text")?.textContent).toContain("Ran the test suite");
    fireEvent.click(screen.getByRole("button", { name: "Technical" }));
    expect(blocked.container.querySelector(".feed-text")?.textContent).toBe("Bash: uv run --extra dev pytest -q");
  });
});

describe("feed view storage helpers", () => {
  it("defaults to plain and tolerates throwing storage", () => {
    expect(loadFeedView({ getItem: () => null, setItem: () => {} })).toBe("plain");
    expect(loadFeedView({ getItem: () => "technical", setItem: () => {} })).toBe("technical");
    expect(loadFeedView({ getItem: () => "garbage", setItem: () => {} })).toBe("plain");
    const bad = { getItem: () => { throw new Error("x"); }, setItem: () => { throw new Error("x"); } };
    expect(loadFeedView(bad)).toBe("plain");
    expect(() => saveFeedView("technical", bad)).not.toThrow();
  });
});

describe("filters match plain and technical", () => {
  const f = (text: string, view?: FeedView) => ({ ...noFilters, text, view });
  it("plain mode matches either form of a feed entry", () => {
    const e = entry();
    expect(matchEntry(f("test suite"), e, new Set())).toBe(true);
    expect(matchEntry(f("pytest"), e, new Set())).toBe(true);
    expect(matchEntry(f("nothing"), e, new Set())).toBe(false);
    expect(matchEntry(f("pytest"), entry({ plain: undefined }), new Set())).toBe(true);
  });
  it("technical mode matches only the technical text", () => {
    expect(matchEntry(f("test suite", "technical"), entry(), new Set())).toBe(false);
    expect(matchEntry(f("pytest", "technical"), entry(), new Set())).toBe(true);
  });
  it("keeps tool-name filtering working", () => {
    expect(matchEntry({ ...noFilters, tools: ["Bash"] }, entry(), new Set())).toBe(true);
    expect(matchEntry({ ...noFilters, tools: ["Read"] }, entry(), new Set())).toBe(false);
  });
  it("matches call plain and outcome by mode", () => {
    const c = call();
    expect(matchCall(f("489 passed"), c)).toBe(true);
    expect(matchCall(f("test suite"), c)).toBe(true);
    expect(matchCall(f("zzz"), c)).toBe(false);
    expect(matchCall(f("489 passed", "technical"), c)).toBe(false);
    expect(matchCall(f("pytest", "technical"), c)).toBe(true);
  });
});
