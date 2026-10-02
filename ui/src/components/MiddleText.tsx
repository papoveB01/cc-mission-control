const TAIL = 6;

/** Ellipsis in the middle so the end (often a lane number) stays visible. Full text in the title. */
export function MiddleText({ text, className }: { text: string; className?: string }) {
  if (text.length <= TAIL + 4) {
    return (
      <span className={className} title={text}>
        {text}
      </span>
    );
  }
  return (
    <span className={`mid${className ? ` ${className}` : ""}`} title={text}>
      <span className="mid-a">{text.slice(0, text.length - TAIL)}</span>
      <span className="mid-b">{text.slice(text.length - TAIL)}</span>
    </span>
  );
}
