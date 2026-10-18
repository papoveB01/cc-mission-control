import type { FileAccess, FileOp } from "./types";

export type SortKey = "path" | "op" | "agent" | "time" | "status";
export type SortDir = "asc" | "desc";

export interface FileView {
  op: FileOp | "all";
  /** Case-insensitive path substring. */
  q: string;
  sort: SortKey;
  dir: SortDir;
}

/** Newest first, like clicking the Time column (see nextSort). */
export const defaultFileView: FileView = { op: "all", q: "", sort: "time", dir: "desc" };

export function filterFiles(rows: readonly FileAccess[], op: FileView["op"], q: string): FileAccess[] {
  const needle = q.trim().toLowerCase();
  return rows.filter((r) => (op === "all" || r.op === op) && (needle === "" || r.path.toLowerCase().includes(needle)));
}

const cmp = (a: string | number, b: string | number): number => (a < b ? -1 : a > b ? 1 : 0);

/** Stable sort; ties fall back to time, then call id, so the order never jumps. */
export function sortFiles(rows: readonly FileAccess[], key: SortKey, dir: SortDir, agentLabel: (id: string) => string): FileAccess[] {
  const val = (r: FileAccess): string | number => {
    switch (key) {
      case "path": return r.path;
      case "op": return r.op;
      case "agent": return agentLabel(r.agent_id).toLowerCase();
      case "status": return r.status;
      case "time": return r.t;
    }
  };
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => sign * cmp(val(a), val(b)) || cmp(a.t, b.t) || cmp(a.call_id, b.call_id));
}

export function viewFiles(rows: readonly FileAccess[], v: FileView, agentLabel: (id: string) => string): FileAccess[] {
  return sortFiles(filterFiles(rows, v.op, v.q), v.sort, v.dir, agentLabel);
}

/** Clicking the active column flips the direction; a new column starts ascending (time starts newest first). */
export function nextSort(v: FileView, key: SortKey): FileView {
  if (v.sort === key) return { ...v, dir: v.dir === "asc" ? "desc" : "asc" };
  return { ...v, sort: key, dir: key === "time" ? "desc" : "asc" };
}
