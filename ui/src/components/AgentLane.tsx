import { createContext, memo, useCallback, useContext, useEffect, useState } from "react";
import { filtersActive, matchCall, type Filters } from "../filters";
import { isStale } from "../store";
import { plural } from "../format";
import type { Agent, AgentStatus, ToolCall } from "../types";
import { ClampText } from "./ClampText";
import { AnimatedNumber } from "./AnimatedNumber";
import { Dial } from "./Dial";
import { Elapsed } from "./Elapsed";
import { ToolCallRow } from "./ToolCallRow";

export const LaneLabels = createContext<ReadonlyMap<string, string>>(new Map());

const STATUS_TEXT: Record<AgentStatus, string> = {
  idle: "Idle",
  running: "Running",
  waiting: "Waiting for input",
  done: "Done",
  error: "Error",
};

const VISIBLE_CALLS = 8;

/** Status glyphs so state is never color-only. */
const GLYPH: Record<AgentStatus, string> = { idle: "-", running: ">", waiting: "?", done: "+", error: "x" };

interface Props {
  agent: Agent;
  /** Epoch seconds when the owning session ended, or null while it is active. */
  sessionEnded: number | null;
  highlighted: boolean;
  filters: Filters;
  onOpenCall: (call: ToolCall, trigger: Element) => void;
  onOpenAgent: (agentId: string, trigger?: Element) => void;
}

export const AgentLane = memo(function AgentLane({ agent, sessionEnded, highlighted, filters, onOpenCall, onOpenAgent }: Props) {
  const names = useContext(LaneLabels);
  const isMain = agent.id === "main";
  const name = isMain ? agent.label : (names.get(agent.id) ?? agent.label);
  const stale = isStale(agent, sessionEnded);
  const finished = !isMain && (agent.status === "done" || agent.status === "error");
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (highlighted) setExpanded(true);
  }, [highlighted]);

  const end = agent.ended ?? sessionEnded;
  const collapsed = finished && !expanded;
  const [born] = useState(() => !finished && Date.now() / 1000 - agent.started < 6);
  const cls = `lane card status-${agent.status}${isMain ? "" : " sub"}${highlighted ? " flash" : ""}${born ? " born" : ""}`;

  const toggle = (): void => setExpanded((x) => !x);
  const head = (
    <div
      className={`lane-head${finished ? " clickable" : ""}`}
      onClick={finished ? (e) => { if (!(e.target as HTMLElement).closest("button")) toggle(); } : undefined}
    >
      <span className="edge" aria-hidden="true" />
      {finished ? (
        <button type="button" className="chev" aria-expanded={!collapsed} aria-label={`${collapsed ? "Expand" : "Collapse"} ${name}`} onClick={toggle}>
          {collapsed ? "+" : "-"}
        </button>
      ) : (
        <span className="lane-glyph" aria-hidden="true">{GLYPH[agent.status]}</span>
      )}
      <button type="button" className="lane-label" title="Open agent details" onClick={(e) => onOpenAgent(agent.id, e.currentTarget)}>
        {name}
      </button>
      {isMain ? null : <span className="chip chip-sub">Sub</span>}
      <span className="lane-status">{STATUS_TEXT[agent.status]}</span>
      <span className="lane-elapsed">
        <Elapsed start={agent.started} end={end} />
      </span>
      {finished ? <span className="lane-count tnum">{plural(agent.total_calls, "call")}</span> : null}
    </div>
  );
  const jumpSpawn = useCallback((id: string, el: Element): void => onOpenAgent(id, el), [onOpenAgent]);
  const filtering = filtersActive(filters);

  if (collapsed) {
    return (
      <section id={`lane-${agent.id}`} className={`${cls} collapsed`} aria-label={`${name} lane`}>
        {head}
      </section>
    );
  }

  const shown = agent.calls.slice(-VISIBLE_CALLS).reverse();
  const tools = Object.entries(agent.tool_counts);

  return (
    <section id={`lane-${agent.id}`} className={cls} aria-label={`${name} lane`}>
      {head}
      <div className="lane-body">
        <div className="lane-top">
          <Dial tokens={agent.context_tokens} window={agent.context_window} />
          <div className="lane-info">
            <ClampText label="Task" text={agent.task} />
            <div className="stats">
              <span className="stat">
                <span className="hud-label">Calls</span> <AnimatedNumber value={agent.total_calls} />
              </span>
              <span className={`stat${agent.errors > 0 ? " tone-err" : ""}`}>
                <span className="hud-label">Errors</span> <AnimatedNumber value={agent.errors} />
              </span>
              {agent.model ? <span className="stat mono muted">{agent.model}</span> : null}
            </div>
          </div>
        </div>
        <div className="tally" aria-label="Tool calls by tool">
          {tools.map(([tool, n]) => (
            <span className="chip tnum" key={tool} title={tool}>
              {tool} {n}
            </span>
          ))}
          {agent.errors > 0 ? <span className="chip chip-err tnum">err {agent.errors}</span> : null}
          {tools.length === 0 && agent.errors === 0 ? <span className="muted">No tool calls yet</span> : null}
        </div>
        {shown.length > 0 ? (
          <ul className="calls">
            {shown.map((c) => (
              <ToolCallRow
                key={c.id}
                call={c}
                dim={filtering && !matchCall(filters, c)}
                stale={stale}
                cap={end}
                spawnLabel={c.subagent_id ? (names.get(c.subagent_id) ?? null) : null}
                onOpen={onOpenCall}
                onJump={jumpSpawn}
              />
            ))}
          </ul>
        ) : null}
        {finished ? <ClampText label="Result" text={agent.result} /> : null}
      </div>
    </section>
  );
});
