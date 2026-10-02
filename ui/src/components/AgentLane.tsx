import { createContext, memo, useContext, useEffect, useState } from "react";
import { isStale } from "../store";
import { plural } from "../format";
import type { Agent, AgentStatus, ToolCall } from "../types";
import { ClampText } from "./ClampText";
import { ContextGauge } from "./ContextGauge";
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

interface Props {
  agent: Agent;
  /** Epoch seconds when the owning session ended, or null while it is active. */
  sessionEnded: number | null;
  highlighted: boolean;
  onOpenCall: (call: ToolCall) => void;
  onJump: (agentId: string) => void;
}

export const AgentLane = memo(function AgentLane({ agent, sessionEnded, highlighted, onOpenCall, onJump }: Props) {
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
  const cls = `lane status-${agent.status}${isMain ? "" : " sub"}${highlighted ? " flash" : ""}`;

  const header = (
    <>
      <span className="edge" aria-hidden="true" />
      <span className="lane-label">{name}</span>
      {isMain ? null : <span className="chip chip-sub">subagent</span>}
      <span className="lane-status">{STATUS_TEXT[agent.status]}</span>
      <span className="lane-elapsed">
        <Elapsed start={agent.started} end={end} />
      </span>
    </>
  );

  if (collapsed) {
    return (
      <section id={`lane-${agent.id}`} className={`${cls} collapsed`} aria-label={`${name} lane`}>
        <button type="button" className="lane-head lane-toggle" aria-expanded="false" onClick={() => setExpanded(true)}>
          {header}
          <span className="lane-count tnum">{plural(agent.total_calls, "call")}</span>
        </button>
      </section>
    );
  }

  const shown = agent.calls.slice(-VISIBLE_CALLS).reverse();
  const tools = Object.entries(agent.tool_counts);

  return (
    <section id={`lane-${agent.id}`} className={cls} aria-label={`${name} lane`}>
      {finished ? (
        <button type="button" className="lane-head lane-toggle" aria-expanded="true" onClick={() => setExpanded(false)}>
          {header}
          <span className="lane-count tnum">{plural(agent.total_calls, "call")}</span>
        </button>
      ) : (
        <div className="lane-head">{header}</div>
      )}
      <div className="lane-body">
        <ClampText label="Task" text={agent.task} />
        <ContextGauge tokens={agent.context_tokens} window={agent.context_window} />
        <div className="tally" aria-label="Tool calls by tool">
          {tools.map(([tool, n]) => (
            <span className="chip tnum" key={tool}>
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
                stale={stale}
                cap={end}
                spawnLabel={c.subagent_id ? (names.get(c.subagent_id) ?? null) : null}
                onOpen={onOpenCall}
                onJump={onJump}
              />
            ))}
          </ul>
        ) : null}
        {finished ? <ClampText label="Result" text={agent.result} /> : null}
      </div>
    </section>
  );
});
