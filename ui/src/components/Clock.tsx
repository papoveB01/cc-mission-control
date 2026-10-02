import { useNow } from "../clock";
import { formatClockSeconds } from "../format";

export function Clock() {
  const now = useNow();
  return <time className="clock tnum" dateTime={new Date(now).toISOString()}>{formatClockSeconds(now / 1000)}</time>;
}
