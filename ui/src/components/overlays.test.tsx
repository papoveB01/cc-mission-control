// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Agent, Session } from "../types";
import { AgentModal } from "./AgentModal";
import { CallDrawer } from "./CallDrawer";
import { Palette } from "./Palette";

const agent = (over: Partial<Agent> = {}): Agent => ({
  id: "main", label: "Main", agent_type: null, status: "running", task: "Fix the bug", result: "", started: 1, ended: null,
  context_tokens: 1000, context_window: 200000, model: "m", tool_counts: {}, errors: 0, total_calls: 0, calls: [], ...over,
});
const session = (agents: Agent[]): Session => ({
  id: "s", title: "proj", cwd: "/p", model: null, status: "active", started: 1, ended: null, last_event: 2, compactions: 0, agents, activity: [],
});
const names = new Map([["main", "Main"], ["sub", "Explore"]]);
const noop = (): void => {};

function modalProps(s: Session, agentId: string, extra: Partial<Parameters<typeof AgentModal>[0]> = {}) {
  return {
    session: s, agentId, names, history: [], suspended: false, returnTo: null, skipReturn: { current: false },
    onClose: noop, onShowLane: noop, onOpenAgent: noop, onOpenCall: noop, ...extra,
  };
}

beforeEach(() => {
  (globalThis as { CSS?: unknown }).CSS = { escape: (s: string) => s.replace(/[^\w-]/g, "\\$&") };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    status: 200, ok: true,
    json: async () => ({ id: "c1", tool: "Bash", status: "ok", started: 1, ended: 2, duration_ms: 1000, input: { command: "ls" }, output: "ok", error: null }),
  })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("AgentModal focus return", () => {
  it("returns focus to the trigger when it is still in the page", () => {
    const trig = document.createElement("button");
    document.body.append(trig);
    trig.focus();
    const view = render(<AgentModal {...modalProps(session([agent()]), "main", { returnTo: trig })} />);
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
    view.unmount();
    expect(document.activeElement).toBe(trig);
  });

  it("falls back to the agent's topology node when the trigger was unmounted", () => {
    const trig = document.createElement("button");
    const node = document.createElement("button");
    node.setAttribute("data-node-id", "main");
    document.body.append(trig, node);
    trig.focus();
    const view = render(<AgentModal {...modalProps(session([agent()]), "main", { returnTo: trig })} />);
    trig.remove();
    view.unmount();
    expect(document.activeElement).toBe(node);
  });

  it("falls back to the lane title, then the main landmark", () => {
    const trig = document.createElement("button");
    const main = document.createElement("main");
    main.id = "session-panel";
    main.tabIndex = -1;
    document.body.append(trig, main);
    trig.focus();
    let view = render(<AgentModal {...modalProps(session([agent()]), "main", { returnTo: trig })} />);
    trig.remove();
    view.unmount();
    expect(document.activeElement).toBe(main);

    const lane = document.createElement("section");
    lane.id = "lane-main";
    const label = document.createElement("button");
    label.className = "lane-label";
    lane.append(label);
    document.body.append(lane);
    const trig2 = document.createElement("button");
    document.body.append(trig2);
    trig2.focus();
    view = render(<AgentModal {...modalProps(session([agent()]), "main", { returnTo: trig2 })} />);
    trig2.remove();
    view.unmount();
    expect(document.activeElement).toBe(label);
  });

  it("does not steal focus back after Show lane", () => {
    const trig = document.createElement("button");
    const other = document.createElement("button");
    document.body.append(trig, other);
    trig.focus();
    const skip = { current: false };
    const view = render(<AgentModal {...modalProps(session([agent()]), "main", { returnTo: trig, skipReturn: skip })} />);
    skip.current = true;
    other.focus();
    view.unmount();
    expect(document.activeElement).toBe(other);
  });
});

describe("AgentModal when the agent disappears", () => {
  it("shows the gone state and keeps focus inside the dialog", () => {
    const withSub = session([agent(), agent({ id: "sub", label: "Explore", agent_type: "Explore" })]);
    const view = render(<AgentModal {...modalProps(withSub, "sub")} />);
    const showLane = screen.getByRole("button", { name: "Show lane" });
    showLane.focus();
    expect(document.activeElement).toBe(showLane);
    view.rerender(<AgentModal {...modalProps(session([agent()]), "sub")} />);
    expect(screen.getByText("This agent is no longer in memory.")).toBeTruthy();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });
});

describe("Esc layering", () => {
  function Harness({ log }: { log: string[] }) {
    const [palette, setPalette] = useState(true);
    const [drawer, setDrawer] = useState(true);
    const [modal, setModal] = useState(true);
    const s = session([agent()]);
    return (
      <>
        {modal ? <AgentModal {...modalProps(s, "main", { suspended: drawer || palette, onClose: () => { log.push("modal"); setModal(false); } })} /> : null}
        {drawer ? <CallDrawer sessionId="s" callId="c1" tool="Bash" status="ok" suspended={palette} onClose={() => { log.push("drawer"); setDrawer(false); }} /> : null}
        {palette ? (
          <Palette ctx={{ sessions: [s], selectedId: "s", names }} skipReturn={{ current: false }} onClose={() => { log.push("palette"); setPalette(false); }} onRun={noop} />
        ) : null}
      </>
    );
  }

  it("closes the palette, then the drawer, then the modal, one per press", async () => {
    const log: string[] = [];
    render(<Harness log={log} />);
    await act(async () => {});
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(log).toEqual(["palette"]);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(log).toEqual(["palette", "drawer"]);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(log).toEqual(["palette", "drawer", "modal"]);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(log).toEqual(["palette", "drawer", "modal"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("Palette", () => {
  it("runs the highlighted result on Enter and moves with the arrow keys", () => {
    const run = vi.fn();
    const s = session([agent(), agent({ id: "sub", label: "Explore" })]);
    render(<Palette ctx={{ sessions: [s], selectedId: "s", names }} skipReturn={{ current: false }} onClose={noop} onRun={run} />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "expl" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(run).toHaveBeenCalledTimes(1);
    expect(run.mock.calls[0]?.[0].target).toEqual({ type: "agent", id: "sub" });
    fireEvent.change(input, { target: { value: "" } });
    const first = input.getAttribute("aria-activedescendant");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).not.toBe(first);
  });
});
