import { memo } from "react";
import { gaugeLevel, formatTokens, gaugePercent } from "../format";
import { sparkGeometry } from "../spark";
import { historyKey, type History, type HistoryPoint } from "../store";
import type { Agent } from "../types";
import { MiddleText } from "./MiddleText";

const W = 160;
const H = 28;

interface RowProps {
  id: string;
  name: string;
  onOpen: (id: string, trigger: Element) => void;
  tokens: number | null;
  window: number | null;
  points: readonly HistoryPoint[];
}

const Row = memo(function Row({ id, name, onOpen, tokens, window, points }: RowProps) {
  const g = sparkGeometry(points, W, H, window);
  const hasWindow = window !== null && window > 0;
  const level = tokens !== null && hasWindow ? gaugeLevel(tokens / window) : "ok";
  const pct = tokens !== null && hasWindow ? gaugePercent(tokens, window) : null;
  return (
    <li>
      <button type="button" className="spark-row" onClick={(e) => onOpen(id, e.currentTarget)} aria-label={`${name}: open agent details`}>
      <MiddleText className="spark-name" text={name} />
      <svg className={`spark level-${level}`} viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={tokens === null ? `${name} context not available` : `${name} context history, now ${formatTokens(tokens)}${pct !== null ? `, ${pct} percent` : ""}${g.drops.length ? `, ${g.drops.length} compactions` : ""}`}>
        <line className="spark-base" x1="0" y1={H - 1} x2={W} y2={H - 1} />
        {g.line ? <path className="spark-line" d={g.line} /> : null}
        {g.drops.map((x, i) => (
          <line key={i} className="spark-drop" x1={x} y1="0" x2={x} y2={H} />
        ))}
        {g.last ? <circle className="spark-dot" cx={g.last.x} cy={g.last.y} r="2.5" /> : null}
      </svg>
      <span className="spark-val tnum">{tokens === null ? "n/a" : pct !== null ? `${pct}%` : formatTokens(tokens)}</span>
      </button>
    </li>
  );
});

interface Props {
  sessionId: string;
  agents: Agent[];
  names: ReadonlyMap<string, string>;
  history: History;
  onOpenAgent: (id: string, trigger: Element) => void;
}

export const ContextPanel = memo(function ContextPanel({ sessionId, agents, names, history, onOpenAgent }: Props) {
  return (
    <ul className="spark-list">
      {agents.map((a) => (
        <Row
          key={a.id}
          id={a.id}
          onOpen={onOpenAgent}
          name={a.id === "main" ? "Main" : (names.get(a.id) ?? a.label)}
          tokens={a.context_tokens}
          window={a.context_window}
          points={history.get(historyKey(sessionId, a.id)) ?? EMPTY}
        />
      ))}
    </ul>
  );
});

const EMPTY: readonly HistoryPoint[] = [];
