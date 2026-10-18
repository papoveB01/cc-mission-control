import { useMemo } from "react";
import { MiddleText } from "../components/MiddleText";
import { formatTokens } from "../format";
import { laneNames } from "../store";
import { formatExact } from "./fmt";
import type { HistorySession } from "./types";
import { barSegments, buildUsage, type UsageRow } from "./usage";

const SERIES = [
  { key: "input", label: "Input" },
  { key: "cache_creation", label: "Cache write" },
  { key: "cache_read", label: "Cache read" },
  { key: "output", label: "Output" },
] as const;

function Num({ n }: { n: number }) {
  return <td className="tnum num" title={formatExact(n)}>{formatTokens(n)}</td>;
}

function Cells({ r }: { r: UsageRow }) {
  return (
    <>
      <Num n={r.input} />
      <Num n={r.cache_creation} />
      <Num n={r.cache_read} />
      <Num n={r.output} />
      <td className="tnum num" title={formatExact(r.messages)}>{formatExact(r.messages)}</td>
      <Num n={r.total} />
    </>
  );
}

/** Token usage per agent, from the hub's usage totals. */
export function UsageTab({ session }: { session: HistorySession }) {
  const names = useMemo(() => laneNames(session.agents), [session.agents]);
  const { rows, total } = useMemo(() => buildUsage(session.usage, session.agents, names), [session, names]);
  const max = Math.max(0, ...rows.map((r) => r.total));

  return (
    <div className="hist-tab">
      <section className="panel" aria-label="Usage">
        <header className="panel-head"><h2 className="hud-title">Usage</h2><span className="muted">Tokens per agent</span></header>
        {rows.length === 0 ? (
          <p className="muted hist-pad">No token usage was recorded for this session.</p>
        ) : (
          <>
            <div className="hist-scroll" tabIndex={0} role="region" aria-label="Usage table">
              <table className="hist-table hist-usage">
                <caption className="sr-only">Token usage per agent. Hover a figure for the exact count.</caption>
                <thead>
                  <tr>
                    <th scope="col">Agent</th>
                    <th scope="col" className="num">Input</th>
                    <th scope="col" className="num">Cache write</th>
                    <th scope="col" className="num">Cache read</th>
                    <th scope="col" className="num">Output</th>
                    <th scope="col" className="num">Messages</th>
                    <th scope="col" className="num">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <th scope="row" className="hist-agent"><MiddleText text={r.name} /></th>
                      <Cells r={r} />
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    <Cells r={total} />
                  </tr>
                </tfoot>
              </table>
            </div>
            <div className="hist-bars" role="group" aria-label="Share of tokens per agent">
              <ul className="hist-legend">
                {SERIES.map((s) => (
                  <li key={s.key}><span className={`hist-swatch seg-${s.key}`} aria-hidden="true" />{s.label}</li>
                ))}
              </ul>
              {rows.map((r) => (
                <div className="hist-bar-row" key={r.id}>
                  <MiddleText className="hist-bar-name" text={r.name} />
                  <div
                    className="hist-bar"
                    role="img"
                    aria-label={`${r.name}: ${SERIES.map((s) => `${s.label} ${formatTokens(r[s.key])}`).join(", ")}`}
                  >
                    {barSegments(r, max).map((seg) => (
                      <span key={seg.key} className={`seg-${seg.key}`} style={{ width: `${seg.pct}%` }} />
                    ))}
                  </div>
                  <span className="tnum hist-bar-val">{formatTokens(r.total)}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
