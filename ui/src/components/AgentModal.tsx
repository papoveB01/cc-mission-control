import { memo, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { formatClockSeconds, formatTokens } from "../format";
import { feedItems, feedRowStatus, filterCalls, findAgent, findSpawn, historyStats, liveAgentIds, subagentsOf, type CallFilter, type HistoryPoint } from "../store";
import { sparkGeometry } from "../spark";
import type { Agent, AgentStatus, Session, ToolCall } from "../types";
import { WrapPaths } from "./ActivityFeed";
import { Dial } from "./Dial";
import { Elapsed } from "./Elapsed";
import { ToolCallRow } from "./ToolCallRow";
import { isStale } from "../store";

const STATUS_TEXT: Record<AgentStatus, string> = {
  idle: "Idle",
  running: "Running",
  waiting: "Waiting for input",
  done: "Done",
  error: "Error",
};
const GLYPH: Record<AgentStatus, string> = { idle: "-", running: ">", waiting: "?", done: "+", error: "x" };
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
const FILTERS: { id: CallFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "running", label: "Running" },
  { id: "errors", label: "Errors" },
];

const SW = 440;
const SH = 72;

interface Props {
  session: Session;
  agentId: string;
  names: ReadonlyMap<string, string>;
  history: readonly HistoryPoint[];
  /** The call drawer is open above the modal: stop reacting to Esc. */
  suspended: boolean;
  /** Element to return focus to on close (falls back to the focused element at open). */
  returnTo: Element | null;
  /** Set true by "Show lane" so focus is not yanked back to the trigger. */
  skipReturn: { current: boolean };
  onClose: () => void;
  onShowLane: (agentId: string) => void;
  onOpenAgent: (agentId: string) => void;
  onOpenCall: (call: ToolCall, trigger: Element) => void;
}

const Body = memo(function Body({ session, agent, names, history, onShowLane, onOpenAgent, onOpenCall, onClose }: Omit<Props, "agentId" | "suspended" | "returnTo" | "skipReturn"> & { agent: Agent }) {
  const [status, setStatus] = useState<CallFilter>("all");
  const [text, setText] = useState("");
  const [copied, setCopied] = useState(false);
  const isMain = agent.id === "main";
  const name = isMain ? "Main" : (names.get(agent.id) ?? agent.label);
  const sessionEnded = session.status === "ended" ? (session.ended ?? session.last_event) : null;
  const stale = isStale(agent, sessionEnded);
  const end = agent.ended ?? sessionEnded;
  const stats = historyStats(history);
  const spark = sparkGeometry(history, SW, SH, agent.context_window);
  const spawn = isMain ? null : findSpawn(session.agents, agent.id);
  const parentName = spawn ? (spawn.parentId === "main" ? "Main" : (names.get(spawn.parentId) ?? spawn.parentId)) : null;
  const subs = isMain ? subagentsOf(session.agents) : [];
  const calls = useMemo(() => filterCalls(agent.calls, status, text), [agent.calls, status, text]);
  const live = useMemo(() => liveAgentIds(session.agents), [session.agents]);
  const activity = useMemo(() => feedItems(session.activity).filter((i) => i.entry.agent_id === agent.id), [session.activity, agent.id]);
  const tools = Object.entries(agent.tool_counts);

  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (copyTimer.current) clearTimeout(copyTimer.current); }, []);
  const copy = (): void => {
    const done = (ok: boolean): void => {
      setCopied(ok);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1500);
    };
    try {
      navigator.clipboard.writeText(agent.id).then(() => done(true)).catch(() => done(false));
    } catch {
      done(false);
    }
  };

  return (
    <>
      <div className="modal-head">
        <span className={`modal-status status-${agent.status}`}>
          <span className="lane-glyph" aria-hidden="true">{GLYPH[agent.status]}</span>
          {STATUS_TEXT[agent.status]}
        </span>
        <h2 id="modal-title" className={isMain ? "" : "sub-title"}>{name}</h2>
        {isMain ? null : <span className="chip chip-sub">Subagent</span>}
        <button type="button" className="close-btn" onClick={onClose}>Close</button>
      </div>
      <div className="modal-body">
        <dl className="facts">
          <div><dt>Type</dt><dd className="mono">{agent.agent_type ?? "main session"}</dd></div>
          <div><dt>{end === null ? "Elapsed" : "Duration"}</dt><dd><Elapsed start={agent.started} end={end} /></dd></div>
          <div><dt>Started</dt><dd className="tnum">{formatClockSeconds(agent.started)}</dd></div>
          <div><dt>Model</dt><dd className="mono">{agent.model ?? "unknown"}</dd></div>
          <div>
            <dt>Agent id</dt>
            <dd className="mono id-cell">
              <span className="id-text">{agent.id}</span>
              <button type="button" className="link-btn" onClick={copy}>Copy</button>
              <span className="muted" role="status" aria-live="polite">{copied ? "Copied" : ""}</span>
            </dd>
          </div>
        </dl>

        <section className="m-sec" aria-labelledby="m-ctx">
          <h3 id="m-ctx" className="hud-label">Context</h3>
          <div className="m-ctx">
            <div className="m-dial"><Dial tokens={agent.context_tokens} window={agent.context_window} /></div>
            <div className="m-spark">
              <svg viewBox={`0 0 ${SW} ${SH}`} className="spark spark-large" role="img"
                aria-label={stats ? `Context history: ${stats.count} samples, min ${formatTokens(stats.min)}, max ${formatTokens(stats.max)}, now ${formatTokens(stats.current)}` : "No context history yet"}>
                <line className="spark-base" x1="0" y1={SH - 1} x2={SW} y2={SH - 1} />
                {spark.line ? <path className="spark-line" d={spark.line} /> : null}
                {spark.drops.map((x, i) => <line key={i} className="spark-drop" x1={x} y1="0" x2={x} y2={SH} />)}
                {spark.last ? <circle className="spark-dot" cx={spark.last.x} cy={spark.last.y} r="3" /> : null}
              </svg>
              {stats ? (
                <p className="spark-stats tnum">
                  <span>Min {formatTokens(stats.min)}</span> <span>Max {formatTokens(stats.max)}</span> <span>Now {formatTokens(stats.current)}</span>
                  {spark.drops.length > 0 ? <span>Compactions {spark.drops.length}</span> : null}
                </p>
              ) : (
                <p className="muted">No context samples yet.</p>
              )}
            </div>
          </div>
        </section>

        <section className="m-sec" aria-labelledby="m-task">
          <h3 id="m-task" className="hud-label">Task</h3>
          <p className="m-text">{agent.task || "No task text."}</p>
          {!isMain ? (
            <>
              <h3 className="hud-label m-gap">Result</h3>
              <div className="m-text m-result" tabIndex={0}>{agent.result || "No result yet."}</div>
            </>
          ) : null}
        </section>

        <section className="m-sec" aria-labelledby="m-lin">
          <h3 id="m-lin" className="hud-label">Lineage</h3>
          {isMain ? (
            subs.length === 0 ? (
              <p className="muted">No subagents yet.</p>
            ) : (
              <ul className="m-list">
                {subs.map((a) => (
                  <li key={a.id}>
                    <button type="button" className="topo-item" onClick={() => onOpenAgent(a.id)}>
                      <span className={`swatch st-${a.status}`} aria-hidden="true" />
                      <span className="topo-item-name">{names.get(a.id) ?? a.label}</span>
                      <span className="muted">{STATUS_TEXT[a.status]}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )
          ) : spawn ? (
            <p>
              Spawned by {parentName} via{" "}
              <button type="button" className="link-btn inline" onClick={(e) => onOpenCall(spawn.call, e.currentTarget)}>{spawn.call.tool} call</button>
              <span className="muted mono"> {spawn.call.summary}</span>
            </p>
          ) : (
            <p className="muted">The spawn call is not available.</p>
          )}
        </section>

        <section className="m-sec" aria-labelledby="m-tools">
          <h3 id="m-tools" className="hud-label">Tools</h3>
          <div className="tally">
            {tools.map(([tool, n]) => <span className="chip tnum" key={tool}>{tool} {n}</span>)}
            {agent.errors > 0 ? <span className="chip chip-err tnum">err {agent.errors}</span> : null}
            {tools.length === 0 ? <span className="muted">No tool calls yet</span> : null}
          </div>
          <div className="m-filters">
            <div role="group" aria-label="Filter calls by status" className="chips">
              {FILTERS.map((f) => (
                <button key={f.id} type="button" className={`fchip${status === f.id ? " on" : ""}`} aria-pressed={status === f.id} onClick={() => setStatus(f.id)}>{f.label}</button>
              ))}
            </div>
            <input className="m-input" type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Filter calls" aria-label="Filter calls by text" />
          </div>
          {calls.length === 0 ? (
            <p className="muted">{agent.calls.length === 0 ? "No calls recorded." : "No calls match the filter."}</p>
          ) : (
            <ul className="calls m-calls">
              {calls.map((c) => (
                <ToolCallRow key={c.id} call={c} stale={stale} cap={end}
                  spawnLabel={c.subagent_id ? (names.get(c.subagent_id) ?? null) : null}
                  onOpen={onOpenCall} onJump={onOpenAgent} />
              ))}
            </ul>
          )}
          <p className="muted m-note">Showing the last {agent.calls.length} of {agent.total_calls} calls the dashboard has.</p>
        </section>

        <section className="m-sec" aria-labelledby="m-act">
          <h3 id="m-act" className="hud-label">Activity</h3>
          {activity.length === 0 ? (
            <p className="muted">No activity entries.</p>
          ) : (
            <ol className="m-act">
              {activity.map(({ key, entry }) => {
                const st = feedRowStatus(entry, live);
                return (
                  <li key={key} className={`feed-item st-${st} m-act-item`}>
                    <span className="feed-time tnum">{formatClockSeconds(entry.t)}</span>
                    <span className="feed-text">
                      {st === "error" ? <span className="sr-only">Failed: </span> : null}
                      <WrapPaths text={entry.text} />
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </section>
      </div>
      <div className="modal-foot">
        <button type="button" className="act-btn" onClick={() => onShowLane(agent.id)}>Show lane</button>
        <button type="button" className="act-btn" onClick={onClose}>Close</button>
      </div>
    </>
  );
});

export function AgentModal(props: Props) {
  const { session, agentId, suspended, onClose, returnTo, skipReturn } = props;
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(returnTo ?? document.activeElement);
  const agent = findAgent(session.agents, agentId);

  const firstAgent = useRef(agentId);
  useEffect(() => {
    panel.current?.focus();
    const target = opener.current;
    return () => {
      if (skipReturn.current) {
        skipReturn.current = false;
        return;
      }
      const focusable = (el: Element | null): el is HTMLElement | SVGElement => (el instanceof HTMLElement || el instanceof SVGElement) && el.isConnected;
      if (focusable(target)) return target.focus();
      // The trigger is gone (collapsed lane, regrouped node, popover): fall back to a stable neighbour.
      const id = firstAgent.current;
      const fallbacks = [
        `[data-node-id="${CSS.escape(id)}"]`,
        `[data-node-id="group"]`,
        `#lane-${CSS.escape(id)} .lane-label`,
        `#session-panel`,
      ];
      for (const sel of fallbacks) {
        const el = document.querySelector(sel);
        if (focusable(el)) return el.focus();
      }
    };
  }, [skipReturn]);

  // Keep focus inside when the shown agent changes (the previous button unmounts).
  const present = agent !== null;
  useEffect(() => {
    if (panel.current && !panel.current.contains(document.activeElement)) panel.current.focus();
  }, [agentId, present]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape" && !suspended) {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose, suspended]);

  const trap = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== "Tab" || !panel.current) return;
    const nodes = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((n) => !n.hasAttribute("disabled"));
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (!first || !last) return;
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === panel.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="modal-root">
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={agent ? "modal-title" : "modal-gone"} tabIndex={-1} ref={panel} onKeyDown={trap}>
        {agent ? (
          <Body key={agent.id} {...props} agent={agent} />
        ) : (
          <>
            <div className="modal-head">
              <h2 id="modal-gone">Agent</h2>
              <button type="button" className="close-btn" onClick={onClose}>Close</button>
            </div>
            <div className="modal-body"><p className="muted">This agent is no longer in memory.</p></div>
          </>
        )}
      </div>
    </div>
  );
}
