import { memo } from "react";
import { activeFilterCount, noFilters, toggleIn, type Filters, type StatusFilter } from "../filters";

const STATUSES: { id: StatusFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "errors", label: "Errors" },
  { id: "running", label: "Running" },
];

interface MultiProps {
  label: string;
  options: { id: string; label: string }[];
  selected: string[];
  onToggle: (id: string) => void;
}

function Multi({ label, options, selected, onToggle }: MultiProps) {
  return (
    <details className="msel">
      <summary>
        {label}
        {selected.length > 0 ? <span className="msel-count tnum"> {selected.length}</span> : null}
      </summary>
      <div className="msel-list" role="group" aria-label={label}>
        {options.length === 0 ? <span className="muted">None yet</span> : null}
        {options.map((o) => (
          <label key={o.id} className="msel-opt">
            <input type="checkbox" checked={selected.includes(o.id)} onChange={() => onToggle(o.id)} />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
    </details>
  );
}

interface Props {
  filters: Filters;
  onChange: (f: Filters) => void;
  agentOptions: { id: string; label: string }[];
  toolOptions: string[];
  /** Saved filters that reference agents or tools not in this session were ignored. */
  stale: boolean;
}

export const FeedFilters = memo(function FeedFilters({ filters, onChange, agentOptions, toolOptions, stale }: Props) {
  const count = activeFilterCount(filters);
  const labelOf = (id: string): string => agentOptions.find((a) => a.id === id)?.label ?? id;
  return (
    <div className="ffilters">
      <div className="chips" role="group" aria-label="Filter by status">
        {STATUSES.map((s) => (
          <button key={s.id} type="button" className={`fchip${filters.status === s.id ? " on" : ""}`} aria-pressed={filters.status === s.id} title={s.id === "running" ? "Calls and subagents running now (feed: subagents still running)" : undefined} onClick={() => onChange({ ...filters, status: s.id })}>
            {s.label}
          </button>
        ))}
      </div>
      <div className="ffilters-row">
        <Multi label="Agents" options={agentOptions} selected={filters.agents} onToggle={(id) => onChange({ ...filters, agents: toggleIn(filters.agents, id) })} />
        <Multi label="Tools" options={toolOptions.map((t) => ({ id: t, label: t }))} selected={filters.tools} onToggle={(id) => onChange({ ...filters, tools: toggleIn(filters.tools, id) })} />
        <input
          id="feed-filter-text"
          className="m-input"
          type="search"
          value={filters.text}
          placeholder="Filter text"
          aria-label="Filter feed and calls by text"
          onChange={(e) => onChange({ ...filters, text: e.target.value })}
        />
      </div>
      {stale ? (
        <div className="factive" role="status">
          <span className="muted">Filters reference agents not in this session.</span>
          <button type="button" className="link-btn" onClick={() => onChange(noFilters)}>Clear all</button>
        </div>
      ) : null}
      {count > 0 ? (
        <div className="factive" aria-label="Active filters">
          {filters.status !== "all" ? <span className="chip">{filters.status}</span> : null}
          {filters.agents.map((a) => <span className="chip" key={`a${a}`}>{labelOf(a)}</span>)}
          {filters.tools.map((t) => <span className="chip" key={`t${t}`}>{t}</span>)}
          {filters.text.trim() ? <span className="chip">&quot;{filters.text.trim()}&quot;</span> : null}
          <button type="button" className="link-btn" onClick={() => onChange(noFilters)}>Clear all</button>
        </div>
      ) : null}
    </div>
  );
});
