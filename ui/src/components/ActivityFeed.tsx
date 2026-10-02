import { memo, type ReactNode, useLayoutEffect, useMemo, useRef, useState } from "react";
import { formatClockSeconds } from "../format";
import { filtersActive, matchEntry, type Filters } from "../filters";
import { MiddleText } from "./MiddleText";
import { agentName, feedItems, feedRowStatus } from "../store";
import type { Activity } from "../types";

const AWAY_PX = 24;
const STATUS_PREFIX: Record<string, string> = { error: "Failed", running: "Running", ok: "Succeeded" };
const GLYPH: Record<string, string> = { error: "\u00d7", running: "\u2026" };

/** Wraps at spaces, and allows a break after each "/" inside path-like tokens. */
export function WrapPaths({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\s+)/).map((tok, i) =>
        tok.includes("/") && tok.length > 1 ? (
          <span key={i}>
            {tok.split("/").map((part, j, arr) => (
              <span key={j}>
                {part}
                {j < arr.length - 1 ? (
                  <>
                    /<wbr />
                  </>
                ) : null}
              </span>
            ))}
          </span>
        ) : (
          tok
        ),
      )}
    </>
  );
}

interface Props {
  activity: Activity[];
  names: ReadonlyMap<string, string>;
  liveAgents: ReadonlySet<string>;
  onOpenAgent: (id: string, trigger: Element) => void;
  filters: Filters;
  /** Phase B mounts the filter chips here. */
  toolbar?: ReactNode;
}

export const ActivityFeed = memo(function ActivityFeed({ activity, names, liveAgents, onOpenAgent, filters, toolbar }: Props) {
  const items = useMemo(() => (filtersActive(filters) ? feedItems(activity).filter((i) => matchEntry(filters, i.entry, liveAgents)) : feedItems(activity)), [activity, filters, liveAgents]);
  const scroller = useRef<HTMLOListElement>(null);
  const anchor = useRef<string | null>(null);
  const [away, setAway] = useState(false);

  const newestKey = items[0]?.key ?? null;
  let fresh = 0;
  if (away && anchor.current !== null) {
    const idx = items.findIndex((i) => i.key === anchor.current);
    fresh = idx < 0 ? items.length : idx;
  }

  useLayoutEffect(() => {
    if (!away && scroller.current) scroller.current.scrollTop = 0;
  }, [newestKey, away]);

  const onScroll = (): void => {
    const el = scroller.current;
    if (!el) return;
    const isAway = el.scrollTop > AWAY_PX;
    if (isAway && !away) anchor.current = newestKey;
    if (!isAway) anchor.current = null;
    if (isAway !== away) setAway(isAway);
  };

  const jump = (): void => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scroller.current?.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
  };

  return (
    <aside className="feed panel" aria-label="Activity">
      <h2 className="feed-title hud-title">Activity</h2>
      {toolbar ? <div className="feed-toolbar">{toolbar}</div> : null}
      {fresh > 0 ? (
        <button type="button" className="new-pill" onClick={jump}>
          {fresh} new
        </button>
      ) : null}
      <ol className="feed-list" ref={scroller} onScroll={onScroll} tabIndex={0} aria-label="Activity, newest first">
        {items.map(({ key, entry }) => {
          const status = feedRowStatus(entry, liveAgents);
          return (
          <li key={key} className={`feed-item st-${status}`}>
            <span className="feed-time tnum">{formatClockSeconds(entry.t)}</span>
            {names.has(entry.agent_id) ? (
              <button type="button" className="feed-agent" onClick={(e) => onOpenAgent(entry.agent_id, e.currentTarget)} title={`${agentName(names, entry.agent_id)}: open details`}>
                <MiddleText text={agentName(names, entry.agent_id)} />
              </button>
            ) : (
              <span className="feed-agent">{agentName(names, entry.agent_id)}</span>
            )}
            <span className="feed-text">
              {STATUS_PREFIX[status] ? <span className="sr-only">{STATUS_PREFIX[status]}: </span> : null}
              {GLYPH[status] ? (
                <span className="glyph" aria-hidden="true">
                  {GLYPH[status]}{" "}
                </span>
              ) : null}
              <WrapPaths text={entry.text} />
            </span>
          </li>
          );
        })}
        {items.length === 0 ? <li className="muted feed-empty">{filtersActive(filters) ? "No entries match the filters" : "No activity yet"}</li> : null}
      </ol>
    </aside>
  );
});
