export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export const scrollBehavior = (): ScrollBehavior => (prefersReducedMotion() ? "auto" : "smooth");

/** Ease-out cubic, t in 0..1. */
export const easeOut = (t: number): number => 1 - (1 - t) ** 3;
