import { memo } from "react";
import { DIAL_ROTATE_DEG, dialGeometry } from "../dial";
import { formatTokens, gaugePercent } from "../format";

const R = 40;

interface Props {
  tokens: number | null;
  window: number | null;
}

/** 270 degree radial context dial. Keeps role="meter" semantics when a value exists. */
export const Dial = memo(function Dial({ tokens, window }: Props) {
  const hasWindow = window !== null && window > 0;
  if (tokens === null) {
    return (
      <div className="dial-wrap">
        <svg className="dial level-none" viewBox="0 0 100 100" role="img" aria-label="Context not available">
          <circle className="dial-track" cx="50" cy="50" r={R} strokeDasharray={`${dialGeometry(0, 1, R).track} 999`} transform={`rotate(${DIAL_ROTATE_DEG} 50 50)`} />
          <text className="dial-pct" x="50" y="56" textAnchor="middle">n/a</text>
        </svg>
        <span className="dial-tokens">Context not available</span>
      </div>
    );
  }
  // Without a window there is no meaningful fraction: draw a neutral, empty dial.
  const g = dialGeometry(tokens, hasWindow ? window : 0, R);
  const pct = hasWindow ? gaugePercent(tokens, window) : 0;
  const text = hasWindow ? `${formatTokens(tokens)} / ${formatTokens(window)}` : formatTokens(tokens);
  return (
    <div className="dial-wrap">
      <svg
        className={`dial level-${hasWindow ? g.level : "none"}`}
        viewBox="0 0 100 100"
        role="meter"
        aria-label="Context used"
        aria-valuemin={0}
        aria-valuemax={hasWindow ? window : tokens}
        aria-valuenow={tokens}
        aria-valuetext={hasWindow ? `${text} tokens, ${pct} percent` : `${text} tokens`}
      >
        <circle className="dial-track" cx="50" cy="50" r={R} strokeDasharray={`${g.track} ${g.circumference}`} transform={`rotate(${DIAL_ROTATE_DEG} 50 50)`} />
        {g.fill >= 0.5 ? <circle className="dial-fill" cx="50" cy="50" r={R} strokeDasharray={`${g.fill} ${g.circumference}`} transform={`rotate(${DIAL_ROTATE_DEG} 50 50)`} /> : null}
        <text className="dial-pct tnum" x="50" y="56" textAnchor="middle">
          {hasWindow ? `${pct}%` : formatTokens(tokens)}
        </text>
      </svg>
      <span className="dial-tokens tnum">{text}</span>
    </div>
  );
});
