import { memo, useRef, type KeyboardEvent } from "react";
import { formatClock } from "../format";
import { tabLabels } from "../store";
import type { ConnectionState, Session } from "../types";

const CONN_TEXT: Record<ConnectionState, string> = { live: "Live", reconnecting: "Reconnecting", offline: "Offline" };

interface Props {
  sessions: Session[];
  selectedId: string | null;
  connection: ConnectionState;
  onSelect: (id: string) => void;
}

export const Header = memo(function Header({ sessions, selectedId, connection, onSelect }: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const labels = tabLabels(sessions);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    const idx = sessions.findIndex((s) => s.id === selectedId);
    if (idx < 0 || sessions.length === 0) return;
    let next = idx;
    if (e.key === "ArrowRight") next = (idx + 1) % sessions.length;
    else if (e.key === "ArrowLeft") next = (idx - 1 + sessions.length) % sessions.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = sessions.length - 1;
    else return;
    e.preventDefault();
    const target = sessions[next];
    if (!target) return;
    onSelect(target.id);
    requestAnimationFrame(() => {
      listRef.current?.querySelector<HTMLElement>(`[data-tab-id="${CSS.escape(target.id)}"]`)?.focus();
    });
  };

  return (
    <header className="header">
      <h1 className="brand">Mission Control</h1>
      <div className="tabs" role="tablist" aria-label="Sessions" ref={listRef} onKeyDown={onKeyDown}>
        {sessions.map((s) => {
          const selected = s.id === selectedId;
          return (
            <button
              key={s.id}
              type="button"
              role="tab"
              id={`tab-${s.id}`}
              data-tab-id={s.id}
              aria-selected={selected}
              aria-controls="session-panel"
              tabIndex={selected ? 0 : -1}
              className={`tab${s.status === "ended" ? " ended" : ""}`}
              onClick={() => onSelect(s.id)}
              title={`${s.cwd || s.title} \u00b7 started ${formatClock(s.started)}`}
            >
              {s.status === "active" ? <span className="dot dot-live" aria-hidden="true" /> : null}
              <span className="tab-title">{labels.get(s.id) ?? s.title}</span>
              {s.status === "ended" ? <span className="sr-only"> (ended)</span> : null}
            </button>
          );
        })}
      </div>
      <div className={`conn conn-${connection}`} role="status">
        <span className="dot" aria-hidden="true" />
        {CONN_TEXT[connection]}
      </div>
    </header>
  );
});
