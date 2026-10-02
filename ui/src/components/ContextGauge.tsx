import { memo } from "react";
import { formatTokens, gaugeLevel, gaugePercent } from "../format";

interface Props {
  tokens: number | null;
  window: number | null;
}

export const ContextGauge = memo(function ContextGauge({ tokens, window }: Props) {
  if (tokens === null) {
    return (
      <div className="gauge">
        <span className="label">Context</span>
        <span className="gauge-na">not available</span>
      </div>
    );
  }
  const hasWindow = window !== null && window > 0;
  const fraction = hasWindow ? tokens / window : 0;
  const pct = hasWindow ? gaugePercent(tokens, window) : 0;
  const text = hasWindow ? `${formatTokens(tokens)} / ${formatTokens(window)}` : formatTokens(tokens);
  return (
    <div className="gauge">
      <span className="label">Context</span>
      <div
        className={`gauge-bar level-${gaugeLevel(fraction)}`}
        role="meter"
        aria-label="Context used"
        aria-valuemin={0}
        aria-valuemax={hasWindow ? window : tokens}
        aria-valuenow={tokens}
        aria-valuetext={hasWindow ? `${text} tokens, ${pct} percent` : `${text} tokens`}
      >
        <div className="gauge-fill" style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <span className="gauge-text tnum">
        {text}
        {hasWindow ? <span className="gauge-pct"> {pct}%</span> : null}
      </span>
    </div>
  );
});
