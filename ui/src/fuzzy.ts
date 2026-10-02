/** Acronym matching applies to targets up to this long (agent, session and action names). */
export const ACRONYM_MAX_LEN = 32;

const isBoundary = (t: string, i: number): boolean => i === 0 || /[\s/\-_.:,()]/.test(t.charAt(i - 1));

/**
 * Can the query be spelled as runs that each start at a word start ("gp" -> general-purpose,
 * "cr" -> code-reviewer, "gpurp" -> general-purpose)? Returns the number of runs, or -1.
 */
function acronymRuns(q: string, t: string): number {
  const memo = new Map<number, number>();
  const solve = (qi: number, ti: number): number => {
    if (qi === q.length) return 0;
    const key = qi * 4096 + ti;
    const hit = memo.get(key);
    if (hit !== undefined) return hit;
    let best = -1;
    for (let w = ti; w < t.length && best < 0; w++) {
      if (!isBoundary(t, w) || t.charAt(w) !== q.charAt(qi)) continue;
      let k = 0;
      while (qi + k < q.length && w + k < t.length && t.charAt(w + k) === q.charAt(qi + k)) {
        k += 1;
        const rest = solve(qi + k, w + k);
        if (rest >= 0) {
          best = rest + 1;
          break;
        }
      }
    }
    memo.set(key, best);
    return best;
  };
  return solve(0, 0);
}

/**
 * Fuzzy score, or null for no match. A match is either a contiguous substring (best) or an
 * acronym of word starts; scattered letters do not qualify, so "rev" does not match
 * "Read /Users/dev/...". Earlier, word-start and exact matches score higher; shorter targets win ties.
 */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.trim().toLowerCase();
  if (q === "") return 0;
  const t = text.toLowerCase();
  const at = t.indexOf(q);
  if (at >= 0) {
    let score = 100 - Math.min(at, 60) * 0.5 - t.length * 0.05;
    if (at === 0) score += 40;
    else if (isBoundary(t, at)) score += 20;
    if (t === q) score += 30;
    return score;
  }
  // Acronyms only make sense for short names; in long paths "re" + "v" would match by chance.
  if (t.length > ACRONYM_MAX_LEN) return null;
  const runs = acronymRuns(q, t);
  if (runs < 0) return null;
  return 50 - runs * 8 - t.length * 0.05;
}

export interface Scored<T> {
  item: T;
  score: number;
}

/** Matching items with their scores, best first. An empty query keeps the original order. */
export function rankScored<T>(items: readonly T[], query: string, textOf: (item: T) => string): Scored<T>[] {
  if (query.trim() === "") return items.map((item) => ({ item, score: 0 }));
  const scored: (Scored<T> & { i: number })[] = [];
  items.forEach((item, i) => {
    const score = fuzzyScore(query, textOf(item));
    if (score !== null) scored.push({ item, score, i });
  });
  scored.sort((a, b) => b.score - a.score || a.i - b.i);
  return scored;
}

/** Items matching `query`, best first. */
export function rank<T>(items: readonly T[], query: string, textOf: (item: T) => string): T[] {
  return rankScored(items, query, textOf).map((s) => s.item);
}
