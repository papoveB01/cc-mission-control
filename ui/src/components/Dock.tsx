import { useCallback, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { clampDock, DOCK_MIN, type DockState } from "../dock";

interface Props {
  dock: DockState;
  onChange: (next: DockState) => void;
  follow: boolean;
  onFollow: () => void;
  onZoom: (factor: number) => void;
  onFit: () => void;
  children: ReactNode;
}

/** Bottom dock: collapsible, resizable by its top edge. Hosts the timeline. */
export function Dock({ dock, onChange, follow, onFollow, onZoom, onFit, children }: Props) {
  const start = useRef<{ y: number; h: number } | null>(null);
  // Live height while dragging; the shared (persisted) state is only written on release.
  const [dragH, setDragH] = useState<number | null>(null);
  const height = dragH ?? clampDock(dock.height, window.innerHeight);
  const maxH = Math.max(DOCK_MIN, Math.floor(window.innerHeight * 0.5));

  const onDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    if (dock.collapsed) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    start.current = { y: e.clientY, h: height };
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const s = start.current;
    if (s) setDragH(clampDock(s.h + (s.y - e.clientY), window.innerHeight));
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>): void => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    if (start.current && dragH !== null) onChange({ ...dock, height: dragH });
    start.current = null;
    setDragH(null);
  };
  const onKey = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      const step = e.shiftKey ? 48 : 16;
      if (e.key === "ArrowUp") onChange({ ...dock, height: clampDock(height + step, window.innerHeight) });
      else if (e.key === "ArrowDown") onChange({ ...dock, height: clampDock(height - step, window.innerHeight) });
      else return;
      e.preventDefault();
    },
    [dock, height, onChange],
  );

  return (
    <section className={`dock${dock.collapsed ? " collapsed" : ""}`} id="timeline-dock" aria-label="Timeline" style={dock.collapsed ? undefined : { height }}>
      {dock.collapsed ? null : (
        <div
          className="dock-resize"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize timeline"
          aria-valuemin={DOCK_MIN}
          aria-valuemax={maxH}
          aria-valuenow={height}
          tabIndex={0}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          onKeyDown={onKey}
        />
      )}
      <header className="panel-head dock-head">
        <button type="button" className="dock-toggle" aria-expanded={!dock.collapsed} aria-controls="timeline-dock-body" onClick={() => onChange({ ...dock, collapsed: !dock.collapsed })}>
          <span aria-hidden="true">{dock.collapsed ? "+" : "-"}</span> <h2 className="hud-title">Timeline</h2>
        </button>
        {dock.collapsed ? null : (
          <div className="dock-controls">
            <span className="dock-hint muted">Ctrl + scroll to zoom, drag to pan</span>
            <button type="button" className={`fchip${follow ? " on" : ""}`} aria-pressed={follow} onClick={onFollow}>Follow live</button>
            <button type="button" className="fchip" aria-label="Zoom out" onClick={() => onZoom(1 / 1.6)}>-</button>
            <button type="button" className="fchip" aria-label="Zoom in" onClick={() => onZoom(1.6)}>+</button>
            <button type="button" className="fchip" onClick={onFit}>Fit</button>
          </div>
        )}
      </header>
      {dock.collapsed ? null : (
        <div className="dock-body" id="timeline-dock-body">
          {children}
        </div>
      )}
    </section>
  );
}
