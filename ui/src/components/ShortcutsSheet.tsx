import { SHORTCUT_HELP } from "../shortcuts";
import { useOverlay } from "../useOverlay";

export function ShortcutsSheet({ onClose, returnTo }: { onClose: () => void; returnTo?: Element | null }) {
  const { panel, onKeyDown } = useOverlay(onClose, { returnTo });
  return (
    <div className="palette-root">
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div className="palette sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title" tabIndex={-1} ref={panel} onKeyDown={onKeyDown}>
        <div className="modal-head">
          <h2 id="sheet-title" className="sheet-title">Keyboard shortcuts</h2>
          <button type="button" className="close-btn" onClick={onClose}>Close</button>
        </div>
        <dl className="sheet-list">
          {SHORTCUT_HELP.map((s) => (
            <div key={s.keys} className="sheet-row">
              <dt><kbd>{s.keys}</kbd></dt>
              <dd>{s.label}</dd>
            </div>
          ))}
        </dl>
        <p className="muted sheet-note">Single-key shortcuts are ignored while you type in a field.</p>
      </div>
    </div>
  );
}
