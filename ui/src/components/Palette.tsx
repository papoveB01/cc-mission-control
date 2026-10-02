import { useMemo, useRef, useState } from "react";
import { buildResults, flatten, type PaletteContext, type PaletteItem } from "../palette";
import { useOverlay } from "../useOverlay";

interface Props {
  ctx: PaletteContext;
  skipReturn: { current: boolean };
  onClose: () => void;
  /** `opener` is the element that had focus before the palette opened. */
  onRun: (item: PaletteItem, opener: Element | null) => void;
}

export function Palette({ ctx, skipReturn, onClose, onRun }: Props) {
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const opener = useRef<Element | null>(document.activeElement);
  const { panel, onKeyDown } = useOverlay(onClose, { initialFocus: input, skipReturn });
  const groups = useMemo(() => buildResults(query, ctx), [query, ctx]);
  const flat = useMemo(() => flatten(groups), [groups]);
  // The highlighted result is tracked by id so it survives the list being refreshed.
  const found = flat.findIndex((i) => i.id === activeId);
  const idx = found >= 0 ? found : 0;

  const run = (item: PaletteItem | undefined): void => {
    if (item) onRun(item, opener.current);
  };
  const onInputKey = (e: React.KeyboardEvent): void => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length === 0) return;
      const next = (idx + (e.key === "ArrowDown" ? 1 : -1) + flat.length) % flat.length;
      setActiveId(flat[next]?.id ?? null);
      requestAnimationFrame(() => document.getElementById(`pal-opt-${next}`)?.scrollIntoView({ block: "nearest" }));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(flat[idx]);
    }
  };

  let n = -1;
  return (
    <div className="palette-root">
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette" tabIndex={-1} ref={panel} onKeyDown={onKeyDown}>
        <input
          ref={input}
          className="palette-input"
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls="pal-list"
          aria-activedescendant={flat.length > 0 ? `pal-opt-${idx}` : undefined}
          aria-autocomplete="list"
          placeholder="Search agents, sessions, calls, actions"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActiveId(null);
          }}
          onKeyDown={onInputKey}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="palette-list" id="pal-list" role="listbox" aria-label="Results">
          {groups.map((g) => (
            <div key={g.heading} role="group" aria-label={g.heading}>
              <div className="palette-heading hud-label" aria-hidden="true">{g.heading}</div>
              {g.items.map((item) => {
                n += 1;
                const i = n;
                return (
                  <div
                    key={item.id}
                    id={`pal-opt-${i}`}
                    role="option"
                    aria-selected={i === idx}
                    className={`palette-item${i === idx ? " active" : ""}`}
                    onMouseMove={() => setActiveId(item.id)}
                    onClick={() => run(item)}
                  >
                    <span className="palette-label">{item.label}</span>
                    <span className="palette-detail muted">{item.detail}</span>
                  </div>
                );
              })}
            </div>
          ))}
          {flat.length === 0 ? <p className="muted palette-empty">No matches.</p> : null}
        </div>
        <p className="palette-hint muted" aria-hidden="true">Up/Down to move, Enter to run, Esc to close</p>
      </div>
    </div>
  );
}
