import { useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { formatDuration, formatTokens, plural } from "../format";
import { apiGet, AuthError } from "./api";
import { formatDateTime, formatExact, machineLabel } from "./fmt";
import { filtersActive, filtersToApi, noListFilters, type ListFilters } from "./listFilters";
import { navigate } from "./router";
import { backOf, buildHash, filesHash, sessionHash } from "./route";
import type { MachineRow, SessionPage, SessionRow } from "./types";
import { totalTokens } from "./usage";
import { AuthContext, useApi } from "./useApi";

const FIELD_KEYS = ["machine", "project", "q", "from", "to", "file"] as const;

function Filters({ value, machines }: { value: ListFilters; machines: MachineRow[] }) {
  const [draft, setDraft] = useState<ListFilters>(value);
  const key = FIELD_KEYS.map((k) => value[k]).join("\u0001");
  // Back/forward (or a link) changes the applied filters: show them.
  useEffect(() => setDraft(value), [key]); // eslint-disable-line react-hooks/exhaustive-deps -- `key` captures `value`

  const apply = (next: ListFilters): void => navigate(buildHash({ name: "list", filters: next }), true);
  const submit = (e: FormEvent): void => {
    e.preventDefault();
    apply(draft);
  };
  const set = (k: keyof ListFilters, v: string, now = false): void => {
    const next = { ...draft, [k]: v };
    setDraft(next);
    if (now) apply(next);
  };
  const ids = new Set(machines.map((m) => m.machine_id));

  return (
    <form className="hist-filters panel" onSubmit={submit} role="search" aria-label="Filter sessions">
      <div className="hist-field">
        <label className="hud-label" htmlFor="f-machine">Machine</label>
        <select id="f-machine" className="m-input" value={draft.machine} onChange={(e) => set("machine", e.target.value, true)}>
          <option value="">All machines</option>
          {draft.machine && !ids.has(draft.machine) ? <option value={draft.machine}>{draft.machine}</option> : null}
          {machines.map((m) => (
            <option key={m.machine_id} value={m.machine_id}>{machineLabel(m.user, m.host, m.machine_id)}</option>
          ))}
        </select>
      </div>
      <div className="hist-field">
        <label className="hud-label" htmlFor="f-project">Project</label>
        <input id="f-project" className="m-input" value={draft.project} placeholder="Folder name" onChange={(e) => set("project", e.target.value)} onBlur={() => apply(draft)} />
      </div>
      <div className="hist-field hist-grow">
        <label className="hud-label" htmlFor="f-q">Search</label>
        <input id="f-q" className="m-input" type="search" value={draft.q} placeholder="Name, project or prompt" onChange={(e) => set("q", e.target.value)} onBlur={() => apply(draft)} />
      </div>
      <div className="hist-field">
        <label className="hud-label" htmlFor="f-from">From</label>
        <input id="f-from" className="m-input" type="date" value={draft.from} onChange={(e) => set("from", e.target.value, true)} />
      </div>
      <div className="hist-field">
        <label className="hud-label" htmlFor="f-to">To</label>
        <input id="f-to" className="m-input" type="date" value={draft.to} onChange={(e) => set("to", e.target.value, true)} />
      </div>
      <div className="hist-field hist-grow">
        <label className="hud-label" htmlFor="f-file">Touched file</label>
        <input id="f-file" className="m-input" value={draft.file} placeholder="/path/to/file, or a folder ending in /" onChange={(e) => set("file", e.target.value)} onBlur={() => apply(draft)} />
      </div>
      <div className="hist-actions">
        <button type="submit" className="act-btn">Apply</button>
        {filtersActive(value) || filtersActive(draft) ? (
          <button type="button" className="link-btn inline" onClick={() => apply(noListFilters)}>Clear</button>
        ) : null}
      </div>
    </form>
  );
}

function durationOf(r: SessionRow): number {
  return Math.max(0, ((r.ended ?? r.last_event) - r.started) * 1000);
}

function Row({ r, back }: { r: SessionRow; back: string }) {
  const name = (r.name ?? "").trim() || r.project;
  const tokens = totalTokens(r);
  return (
    <tr>
      <td className="hist-name">
        <a href={sessionHash(r.machine_id, r.session_id, back)} className="hist-link" title={name}>{name}</a>
        <span className="muted hist-sub" title={r.cwd || r.project}>{r.project}</span>
      </td>
      <td title={r.machine_id}>{machineLabel(r.user, r.host, r.machine_id)}</td>
      <td className="tnum">{formatDateTime(r.started)}</td>
      <td className="tnum num">
        {formatDuration(durationOf(r))}
        {r.status === "ended" ? null : <span className="muted"> open</span>}
      </td>
      <td className="tnum num">{r.agents}</td>
      <td className="tnum num">{r.tool_calls}</td>
      <td className={`tnum num${r.errors > 0 ? " tone-err" : ""}`}>{r.errors}</td>
      <td className="tnum num" title={`${formatExact(tokens)} tokens (input, cache and output)`}>{formatTokens(tokens)}</td>
      <td className="tnum num" title={formatExact(r.peak_context)}>{r.peak_context > 0 ? formatTokens(r.peak_context) : "-"}</td>
    </tr>
  );
}

type Paged =
  | { key: string; status: "loading"; rows: SessionRow[] }
  | { key: string; status: "ok"; rows: SessionRow[]; next: string | null; more: boolean; moreError: string | null }
  | { key: string; status: "error"; rows: SessionRow[]; message: string };

export function SessionList({ filters }: { filters: ListFilters }) {
  const requireLogin = useContext(AuthContext);
  const machines = useApi<{ machines: MachineRow[] }>("machines", (s) => apiGet("/machines", undefined, s));
  const fkey = filtersToApi(filters, null).toString();
  const [held, setPage] = useState<Paged>({ key: fkey, status: "loading", rows: [] });
  // Rows held for other filters are never shown: in the render where the filters changed, it is loading.
  const page: Paged = held.key === fkey ? held : { key: fkey, status: "loading", rows: [] };
  const ctl = useRef<AbortController | null>(null);

  const fetchPage = (cursor: string | null, prev: SessionRow[]): void => {
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    apiGet<SessionPage>("/sessions", filtersToApi(filters, cursor), c.signal)
      .then((res) => {
        if (!c.signal.aborted) setPage({ key: fkey, status: "ok", rows: [...prev, ...res.sessions], next: res.next_cursor, more: false, moreError: null });
      })
      .catch((err: unknown) => {
        if (c.signal.aborted) return;
        if (err instanceof AuthError) return requireLogin();
        const message = err instanceof Error ? err.message : "Request failed";
        setPage(cursor ? { key: fkey, status: "ok", rows: prev, next: cursor, more: false, moreError: message } : { key: fkey, status: "error", rows: [], message });
      });
  };

  useEffect(() => {
    setPage({ key: fkey, status: "loading", rows: [] });
    fetchPage(null, []);
    return () => ctl.current?.abort();
  }, [fkey]); // eslint-disable-line react-hooks/exhaustive-deps -- fkey encodes the filters

  const back = backOf({ name: "list", filters });
  const loadMore = (): void => {
    if (page.status !== "ok" || !page.next) return;
    setPage({ ...page, more: true, moreError: null });
    fetchPage(page.next, page.rows);
  };

  return (
    <main className="hist-main" id="main" tabIndex={-1}>
      <h1 className="sr-only">Sessions</h1>
      <Filters value={filters} machines={machines.status === "ok" ? machines.data.machines : []} />
      <p className="muted hist-lookup">
        Looking for a file? <a href={filesHash("")}>Find sessions by file</a>
      </p>
      {page.status === "error" ? (
        <div className="panel hist-note" role="alert">
          <p className="err-text">{page.message}</p>
          <button type="button" className="act-btn" onClick={() => { setPage({ key: fkey, status: "loading", rows: [] }); fetchPage(null, []); }}>Retry</button>
        </div>
      ) : null}
      {page.status === "loading" ? <p className="muted hist-note" role="status">Loading sessions</p> : null}
      {page.status === "ok" && page.rows.length === 0 ? (
        <div className="panel hist-note">
          <p>{filtersActive(filters) ? "No sessions match these filters." : "No sessions have been recorded yet."}</p>
          {filtersActive(filters) ? (
            <button type="button" className="link-btn inline" onClick={() => navigate(buildHash({ name: "list", filters: noListFilters }), true)}>Clear filters</button>
          ) : null}
        </div>
      ) : null}
      {page.rows.length > 0 ? (
        <section className="panel hist-table-panel" aria-label="Sessions">
          <div className="hist-scroll" tabIndex={0} role="region" aria-label="Sessions table">
            <table className="hist-table">
              <caption className="sr-only">{plural(page.rows.length, "session")} shown, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Session</th>
                  <th scope="col">Machine</th>
                  <th scope="col">Started</th>
                  <th scope="col" className="num">Duration</th>
                  <th scope="col" className="num">Agents</th>
                  <th scope="col" className="num">Calls</th>
                  <th scope="col" className="num">Errors</th>
                  <th scope="col" className="num" title="Input, cache and output tokens">Tokens</th>
                  <th scope="col" className="num" title="Largest context size of any agent">Peak ctx</th>
                </tr>
              </thead>
              <tbody>
                {page.rows.map((r) => (
                  <Row key={`${r.machine_id}\u0000${r.session_id}`} r={r} back={back} />
                ))}
              </tbody>
            </table>
          </div>
          {page.status === "ok" && (page.next || page.moreError) ? (
            <div className="hist-more">
              <button type="button" className="act-btn" onClick={loadMore} disabled={page.more}>{page.more ? "Loading" : "Load more"}</button>
              {page.moreError ? <span className="err-text" role="alert">{page.moreError}</span> : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </main>
  );
}
