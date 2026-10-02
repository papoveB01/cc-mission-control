import { memo, useEffect, useRef, useState } from "react";
import { easeOut, prefersReducedMotion } from "../motion";

const DURATION_MS = 400;

/** Counter that eases to its new value. Jumps instantly under prefers-reduced-motion. */
export const AnimatedNumber = memo(function AnimatedNumber({ value }: { value: number }) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  const current = useRef(value);

  useEffect(() => {
    if (prefersReducedMotion() || from.current === value) {
      from.current = value;
      current.current = value;
      setShown(value);
      return;
    }
    const start = performance.now();
    const origin = current.current;
    let raf = 0;
    const step = (now: number): void => {
      const t = Math.min(1, (now - start) / DURATION_MS);
      const v = Math.round(origin + (value - origin) * easeOut(t));
      current.current = v;
      setShown(v);
      if (t < 1) raf = requestAnimationFrame(step);
      else from.current = value;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value]);

  return <span className="tnum">{shown}</span>;
});
