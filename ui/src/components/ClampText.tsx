import { useId, useState } from "react";

/** Text clamped to 2 lines with an expand toggle (shown only for longer text). */
export function ClampText({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  if (!text) return null;
  const long = text.length > 110 || text.includes("\n");
  return (
    <div className="clamp">
      <span className="label">{label}</span>
      <p id={id} className={`clamp-text${open ? " open" : ""}`}>
        {text}
      </p>
      {long ? (
        <button type="button" className="link-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          {open ? "Less" : "More"}
        </button>
      ) : null}
    </div>
  );
}
