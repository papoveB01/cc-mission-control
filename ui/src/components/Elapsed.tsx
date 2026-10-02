import { memo } from "react";
import { useNow } from "../clock";
import { formatDuration } from "../format";

function Live({ start }: { start: number }) {
  const now = useNow();
  return <span className="tnum">{formatDuration(now - start * 1000)}</span>;
}

/** Fixed duration when `end` is known, otherwise a live ticking timer. */
export const Elapsed = memo(function Elapsed({ start, end }: { start: number; end: number | null }) {
  if (end !== null) return <span className="tnum">{formatDuration((end - start) * 1000)}</span>;
  return <Live start={start} />;
});
