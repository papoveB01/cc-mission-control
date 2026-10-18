/** Session-list filters. They live in the hash query so a view can be linked. */
export interface ListFilters {
  machine: string;
  project: string;
  q: string;
  /** Local dates, YYYY-MM-DD, inclusive. */
  from: string;
  to: string;
  /** Touched file: an exact path, or a prefix when it ends with "/". */
  file: string;
}

export const noListFilters: ListFilters = { machine: "", project: "", q: "", from: "", to: "", file: "" };

const KEYS = Object.keys(noListFilters) as (keyof ListFilters)[];
const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function filtersFromParams(p: URLSearchParams): ListFilters {
  const out = { ...noListFilters };
  for (const k of KEYS) {
    const v = p.get(k) ?? "";
    out[k] = v.trim() === "" ? "" : v;
  }
  if (!DATE.test(out.from)) out.from = "";
  if (!DATE.test(out.to)) out.to = "";
  return out;
}

/** Only non-empty filters, in a fixed key order (so the same filters always give the same hash). */
export function filtersToParams(f: ListFilters): URLSearchParams {
  const p = new URLSearchParams();
  for (const k of KEYS) if (f[k].trim() !== "") p.set(k, f[k]);
  return p;
}

export const filtersActive = (f: ListFilters): boolean => KEYS.some((k) => f[k].trim() !== "");

function ymdParts(ymd: string): [number, number, number] | null {
  if (!DATE.test(ymd)) return null;
  const [y, m, d] = ymd.split("-").map(Number);
  return [y as number, m as number, d as number];
}

/** Start of a local calendar day, epoch seconds. */
export function dayStart(ymd: string): number | null {
  const p = ymdParts(ymd);
  return p ? new Date(p[0], p[1] - 1, p[2], 0, 0, 0, 0).getTime() / 1000 : null;
}

/** Last instant of a local calendar day, epoch seconds. */
export function dayEnd(ymd: string): number | null {
  const p = ymdParts(ymd);
  return p ? new Date(p[0], p[1] - 1, p[2] + 1, 0, 0, 0, 0).getTime() / 1000 - 0.001 : null;
}

export const PAGE_SIZE = 50;

/** Query for GET /api/history/sessions. */
export function filtersToApi(f: ListFilters, cursor: string | null, limit = PAGE_SIZE): URLSearchParams {
  const p = new URLSearchParams();
  if (f.machine.trim()) p.set("machine", f.machine.trim());
  if (f.project.trim()) p.set("project", f.project.trim());
  if (f.q.trim()) p.set("q", f.q.trim());
  const from = dayStart(f.from);
  const to = dayEnd(f.to);
  if (from !== null) p.set("from", String(from));
  if (to !== null) p.set("to", String(to));
  if (f.file.trim()) p.set("has_file", f.file.trim());
  if (cursor) p.set("cursor", cursor);
  p.set("limit", String(limit));
  return p;
}
