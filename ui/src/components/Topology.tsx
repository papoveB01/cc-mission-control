import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { MiddleText } from "./MiddleText";
import { gaugeLevel } from "../format";
import { findSpawn } from "../store";
import { contextFraction, layoutTopology, nextPulses, shortName, type TopoNode } from "../topology";
import type { Agent, AgentStatus } from "../types";

const STATUS_TEXT: Record<AgentStatus, string> = {
  idle: "idle",
  running: "running",
  waiting: "waiting for input",
  done: "done",
  error: "failed",
};

interface NodeProps {
  id: string;
  kind: TopoNode["kind"];
  x: number;
  y: number;
  r: number;
  dim: boolean;
  glyph: string;
  name: string;
  status: AgentStatus;
  /** Context percent, or null. */
  pct: number | null;
  calls: number;
  errors: number;
  selected: boolean;
  groupCount: number;
  /** Increments once per new tool call; 0 means never pulsed. */
  pulse: number;
  onActivate: (id: string, el: Element) => void;
  onHover: (id: string | null) => void;
}

const ARC_R_GAP = 4;

const Node = memo(function Node(p: NodeProps) {
  const arcR = p.r + ARC_R_GAP;
  const circ = 2 * Math.PI * arcR;
  const level = p.pct === null ? null : gaugeLevel(p.pct / 100);
  const arcLen = (circ * Math.min(100, p.pct ?? 0)) / 100;
  const label =
    p.kind === "group"
      ? `${p.groupCount} more finished subagents`
      : `${p.name}, ${STATUS_TEXT[p.status]}, ${p.calls} calls${p.pct !== null ? `, context ${p.pct} percent` : ""}`;
  const onKey = (e: KeyboardEvent<SVGGElement>): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      p.onActivate(p.id, e.currentTarget);
    }
  };
  return (
    <g
      className={`node status-${p.status} kind-${p.kind}${p.dim ? " dim" : ""}${p.selected ? " selected" : ""}`}
      transform={`translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`}
      role="button"
      tabIndex={0}
      aria-label={p.kind === "group" ? `${label}. List them.` : `${label}. Open details.`}
      data-node-id={p.id}
      onClick={(e) => p.onActivate(p.id, e.currentTarget)}
      onKeyDown={onKey}
      onMouseEnter={() => p.onHover(p.id)}
      onMouseLeave={() => p.onHover(null)}
      onFocus={() => p.onHover(p.id)}
      onBlur={() => p.onHover(null)}
    >
      <circle className="node-focus" r={arcR + 5} />
      {p.selected ? <circle className="node-sel" r={arcR + 3} /> : null}
      {p.pulse > 0 ? <circle key={p.pulse} className="node-pulse" r={p.r} /> : null}
      <circle className="node-arc-track" r={arcR} />
      {level && arcLen >= 0.5 ? (
        <circle className={`node-arc level-${level}`} r={arcR} strokeDasharray={`${arcLen} ${circ}`} transform="rotate(-90)" />
      ) : null}
      <circle className="node-body" r={p.r} />
      <text className="node-glyph" textAnchor="middle" dy="0.35em">
        {p.glyph}
      </text>
      {p.errors > 0 ? <circle className="node-err" cx={p.r * 0.75} cy={-p.r * 0.75} r="4" /> : null}
    </g>
  );
});

interface Props {
  agents: Agent[];
  names: ReadonlyMap<string, string>;
  /** Agent whose detail modal is open. */
  selected: string | null;
  /** Snapshot counter: a change resets the pulse baseline. */
  epoch: number;
  onOpenAgent: (agentId: string, trigger?: Element) => void;
}

export const Topology = memo(function Topology({ agents, names, selected, epoch, onOpenAgent }: Props) {
  const layout = useMemo(() => layoutTopology(agents), [agents]);
  const [hover, setHover] = useState<string | null>(null);
  const [groupOpen, setGroupOpen] = useState(false);
  const byId = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const pos = useMemo(() => new Map(layout.nodes.map((n) => [n.id, n])), [layout]);
  const nameOf = (a: Agent): string => (a.id === "main" ? "Main" : (names.get(a.id) ?? a.label));

  // Pulses: only increases after the baseline (first render, snapshot) count.
  const prevCounts = useRef<Map<string, number> | null>(null);
  const seenEpoch = useRef(epoch);
  const [pulses, setPulses] = useState<ReadonlyMap<string, number>>(new Map());
  useEffect(() => {
    const reset = seenEpoch.current !== epoch;
    seenEpoch.current = epoch;
    const r = nextPulses(reset ? null : prevCounts.current, agents);
    prevCounts.current = r.counts;
    if (r.pulsed.length > 0) {
      setPulses((m) => {
        const next = new Map(m);
        for (const id of r.pulsed) next.set(id, (next.get(id) ?? 0) + 1);
        return next;
      });
    }
  }, [agents, epoch]);

  // Esc dismisses the tooltip and the group popover.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape") {
        setHover(null);
        setGroupOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const stageRef = useRef<HTMLDivElement>(null);
  const posRef = useRef(pos);
  posRef.current = pos;
  const activate = useCallback(
    (id: string, el: Element) => {
      if (posRef.current.get(id)?.kind === "group") setGroupOpen((o) => !o);
      else onOpenAgent(id, el);
    },
    [onOpenAgent],
  );

  const center = layout.size / 2;
  const subs = layout.nodes.filter((n) => n.kind !== "main");
  const hoverNode = hover ? pos.get(hover) : undefined;
  const hoverAgent = hover ? byId.get(hover) : undefined;
  const hoverSpawn = hoverAgent && hoverAgent.id !== "main" ? findSpawn(agents, hoverAgent.id) : null;
  const order = agents.filter((a) => pos.has(a.id));
  const groupNode = layout.nodes.find((n) => n.kind === "group");
  const grouped = groupNode ? groupNode.members.map((id) => byId.get(id)).filter((a): a is Agent => a !== undefined) : [];

  const pctOf = (a: Agent): number | null => {
    const f = contextFraction(a);
    return f === null ? null : Math.round(f * 100);
  };

  const renderNode = (n: TopoNode, a: Agent | undefined) => {
    if (n.kind === "group") {
      return (
        <Node key="group" id="group" kind="group" x={n.x} y={n.y} r={n.r} dim glyph={`+${n.members.length}`} name="finished"
          status="done" pct={null} calls={0} errors={0} selected={false} groupCount={n.members.length} pulse={0}
          onActivate={activate} onHover={setHover} />
      );
    }
    if (!a) return null;
    return (
      <Node key={a.id} id={a.id} kind={n.kind} x={n.x} y={n.y} r={n.r} dim={n.dim}
        glyph={n.kind === "main" ? "M" : shortName(a.label, nameOf(a))} name={nameOf(a)} status={a.status}
        pct={pctOf(a)} calls={a.total_calls} errors={a.errors} selected={selected === a.id} groupCount={0}
        pulse={pulses.get(a.id) ?? 0} onActivate={activate} onHover={setHover} />
    );
  };

  const tipLeft = hoverNode ? Math.min(78, Math.max(22, (hoverNode.x / layout.size) * 100)) : 0;
  const tipTop = hoverNode ? (hoverNode.y / layout.size) * 100 : 0;

  return (
    <div className="topo">
      <div className="topo-stage" ref={stageRef}>
        <svg className="topo-svg" style={{ maxWidth: Math.max(220, layout.size * 1.25) }} viewBox={`0 0 ${layout.size} ${layout.size}`} role="group" aria-label="Agent topology graph">
          {subs.some((n) => !n.dim) ? (
            <circle className="ring" cx={center} cy={center} r={Math.max(...subs.filter((n) => !n.dim).map((n) => Math.hypot(n.x - center, n.y - center)))} />
          ) : null}
          {subs.some((n) => n.dim) ? (
            <circle className="ring ring-outer" cx={center} cy={center} r={Math.max(...subs.filter((n) => n.dim).map((n) => Math.hypot(n.x - center, n.y - center)))} />
          ) : null}
          {subs.map((n) => (
            <line key={`e-${n.id}`} className={`edge${n.dim ? " dim" : ""}`} x1={center} y1={center} x2={n.x} y2={n.y} />
          ))}
          {order.map((a) => renderNode(pos.get(a.id) as TopoNode, a))}
          {groupNode ? renderNode(groupNode, undefined) : null}
        </svg>
        {hoverNode ? (
          <div className="tooltip" id="topo-tip" role="tooltip" style={{ left: `${tipLeft}%`, top: `${tipTop}%` }}>
            {hoverNode.kind === "group" ? (
              <>
                <strong>{hoverNode.members.length} finished</strong>
                <span className="muted">Older finished agents, grouped. Click to list.</span>
              </>
            ) : hoverAgent ? (
              <>
                <strong>{nameOf(hoverAgent)}</strong>
                <span>
                  {STATUS_TEXT[hoverAgent.status]} · {hoverAgent.total_calls} calls
                  {hoverAgent.errors > 0 ? ` · ${hoverAgent.errors} errors` : ""}
                </span>
                <span>{pctOf(hoverAgent) === null ? "Context n/a" : `Context ${pctOf(hoverAgent)}%`}</span>
                {hoverSpawn ? <span>Spawned via {hoverSpawn.call.tool}</span> : null}
                {hoverAgent.task ? <span className="tip-task">{hoverAgent.task}</span> : null}
              </>
            ) : null}
          </div>
        ) : null}
        {groupOpen && grouped.length > 0 ? (
          <div className="popover" role="group" aria-label="Grouped finished agents">
            <div className="popover-head">
              <span className="hud-label">{grouped.length} finished</span>
              <button type="button" className="link-btn" onClick={() => setGroupOpen(false)}>Close</button>
            </div>
            <ul>
              {grouped.map((a) => (
                <li key={a.id}>
                  <button type="button" className="topo-item" onClick={() => { setGroupOpen(false); onOpenAgent(a.id, stageRef.current?.querySelector('[data-node-id="group"]') ?? undefined); }}>
                    <span className={`swatch st-${a.status}`} aria-hidden="true" />
                    <span className="topo-item-name">{nameOf(a)}</span>
                    <span className="muted">{STATUS_TEXT[a.status]}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>

      <ul className="sr-only" aria-label="Agent topology, text alternative">
        {agents.map((a) => (
          <li key={a.id}>
            {nameOf(a)}: {STATUS_TEXT[a.status]}, {a.total_calls} calls, {a.errors} errors,{" "}
            {pctOf(a) === null ? "context not available" : `context ${pctOf(a)} percent`}
            {a.id === "main" ? ", main agent" : ", subagent of Main"}
          </li>
        ))}
      </ul>

      <ul className="topo-compact" aria-label="Agents">
        {agents.map((a) => (
          <li key={a.id}>
            <button type="button" className={`topo-item status-${a.status}`} onClick={(e) => onOpenAgent(a.id, e.currentTarget)}>
              <span className="swatch" aria-hidden="true" />
              <MiddleText text={nameOf(a)} className="topo-item-name" />
              <span className="muted">{STATUS_TEXT[a.status]}</span>
              <span className="tnum muted">{pctOf(a) === null ? "n/a" : `${pctOf(a)}%`}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
});
