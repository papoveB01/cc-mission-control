import { filtersFromParams, filtersToParams, type ListFilters } from "./listFilters";

export type SessionTab = "hud" | "files" | "usage";
export type FileMode = "exact" | "prefix";

export type Route =
  | { name: "list"; filters: ListFilters }
  | { name: "session"; machine: string; session: string; tab: SessionTab; back: string }
  | { name: "files"; path: string; mode: FileMode; back: string }
  | { name: "notfound"; hash: string };

/** The session list with no filters. */
export const LIST_HASH = "#/";

export const TABS: readonly SessionTab[] = ["hud", "files", "usage"];

/**
 * `back` is the hash path a session was opened from, e.g. "/?machine=a" or "/files?path=x".
 * Anything that is not a plain in-page path is dropped, and so is another session (except for the
 * file lookup, which may return to the session it was opened from: `allowSession`).
 */
export function sanitizeBack(back: string | null | undefined, allowSession = false): string {
  if (!back || !back.startsWith("/") || back.startsWith("//") || (!allowSession && back.startsWith("/s/"))) return "";
  return back;
}

function splitHash(hash: string): { path: string; params: URLSearchParams } {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  const q = raw.indexOf("?");
  const path = q < 0 ? raw : raw.slice(0, q);
  return { path: path === "" ? "/" : path, params: new URLSearchParams(q < 0 ? "" : raw.slice(q + 1)) };
}

export function parseHash(hash: string): Route {
  const { path, params } = splitHash(hash);
  try {
    if (path === "/") return { name: "list", filters: filtersFromParams(params) };
    if (path === "/files") {
      const prefix = params.get("prefix");
      const back = sanitizeBack(params.get("back"), true);
      if (prefix) return { name: "files", path: prefix, mode: "prefix", back };
      return { name: "files", path: params.get("path") ?? "", mode: "exact", back };
    }
    const parts = path.split("/");
    if (parts.length === 4 && parts[0] === "" && parts[1] === "s") {
      // Split first, decode after: an encoded "/" inside an id must stay inside its segment.
      const machine = decodeURIComponent(parts[2] as string);
      const session = decodeURIComponent(parts[3] as string);
      if (machine && session) {
        const t = params.get("tab");
        const tab = TABS.find((x) => x === t) ?? "hud";
        return { name: "session", machine, session, tab, back: sanitizeBack(params.get("back")) };
      }
    }
  } catch {
    /* malformed percent-encoding falls through to not found */
  }
  return { name: "notfound", hash };
}

export function buildHash(route: Route): string {
  switch (route.name) {
    case "list": {
      const q = filtersToParams(route.filters).toString();
      return `#/${q ? `?${q}` : ""}`;
    }
    case "session": {
      const p = new URLSearchParams();
      if (route.tab !== "hud") p.set("tab", route.tab);
      const back = sanitizeBack(route.back);
      if (back) p.set("back", back);
      const q = p.toString();
      return `#/s/${encodeURIComponent(route.machine)}/${encodeURIComponent(route.session)}${q ? `?${q}` : ""}`;
    }
    case "files": {
      const p = new URLSearchParams();
      if (route.path) p.set(route.mode === "prefix" ? "prefix" : "path", route.path);
      const back = sanitizeBack(route.back, true);
      if (back) p.set("back", back);
      const q = p.toString();
      return `#/files${q ? `?${q}` : ""}`;
    }
    case "notfound":
      return route.hash;
  }
}

/** The route as a "back" value for a session opened from it. */
export function backOf(route: Route): string {
  const h = buildHash(route);
  return sanitizeBack(h.startsWith("#") ? h.slice(1) : h);
}

export const sessionHash = (machine: string, session: string, back = "", tab: SessionTab = "hud"): string =>
  buildHash({ name: "session", machine, session, tab, back });

export const filesHash = (path: string, mode: FileMode = "exact", back = ""): string => buildHash({ name: "files", path, mode, back });
