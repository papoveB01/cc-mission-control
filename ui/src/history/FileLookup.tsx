import { useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { MiddleText } from "../components/MiddleText";
import { plural } from "../format";
import { apiGet, AuthError } from "./api";
import { formatDateTime } from "./fmt";
import { navigate } from "./router";
import { filesHash, sessionHash, type FileMode, backOf } from "./route";
import type { FileHit, FilePage } from "./types";
import { AuthContext } from "./useApi";

type State =
  | { key: string; status: "idle" }
  | { key: string; status: "loading"; rows: FileHit[] }
  | { key: string; status: "ok"; rows: FileHit[]; next: string | null; more: boolean; moreError: string | null }
  | { key: string; status: "error"; message: string };

const LIMIT = 50;

export function FileLookup({ path, mode, back: from }: { path: string; mode: FileMode; back: string }) {
  const requireLogin = useContext(AuthContext);
  const [draft, setDraft] = useState(path);
  const [draftMode, setDraftMode] = useState<FileMode>(mode);
  const qkey = `${mode}:${path}`;
  const [held, setState] = useState<State>({ key: qkey, status: "idle" });
  // Results held for another path or mode are never shown.
  const state: State = held.key === qkey ? held : path ? { key: qkey, status: "loading", rows: [] } : { key: qkey, status: "idle" };
  const ctl = useRef<AbortController | null>(null);
  const back = backOf({ name: "files", path, mode, back: from });

  useEffect(() => {
    setDraft(path);
    setDraftMode(mode);
  }, [path, mode]);

  const fetchPage = (cursor: string | null, prev: FileHit[]): void => {
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    const p = new URLSearchParams();
    p.set(mode === "prefix" ? "prefix" : "path", path);
    p.set("limit", String(LIMIT));
    if (cursor) p.set("cursor", cursor);
    apiGet<FilePage>("/files", p, c.signal)
      .then((res) => {
        if (!c.signal.aborted) setState({ key: qkey, status: "ok", rows: [...prev, ...res.files], next: res.next_cursor, more: false, moreError: null });
      })
      .catch((err: unknown) => {
        if (c.signal.aborted) return;
        if (err instanceof AuthError) return requireLogin();
        const message = err instanceof Error ? err.message : "Request failed";
        setState(cursor ? { key: qkey, status: "ok", rows: prev, next: cursor, more: false, moreError: message } : { key: qkey, status: "error", message });
      });
  };

  useEffect(() => {
    if (!path) {
      setState({ key: qkey, status: "idle" });
      return;
    }
    setState({ key: qkey, status: "loading", rows: [] });
    fetchPage(null, []);
    return () => ctl.current?.abort();
  }, [path, mode]); // eslint-disable-line react-hooks/exhaustive-deps -- fetchPage reads path and mode

  const submit = (e: FormEvent): void => {
    e.preventDefault();
    navigate(filesHash(draft.trim(), draftMode, from));
  };

  return (
    <main className="hist-main" id="main" tabIndex={-1}>
      <h1 className="sr-only">File lookup</h1>
      <p><a href={from ? `#${from}` : "#/"} className="hist-back">{from.startsWith("/s/") ? "Back to session" : "Back to sessions"}</a></p>
      <form className="hist-filters panel" onSubmit={submit} role="search" aria-label="Find sessions by file">
        <div className="hist-field hist-grow">
          <label className="hud-label" htmlFor="lookup-path">File path</label>
          <input id="lookup-path" className="m-input" value={draft} spellCheck={false} placeholder={draftMode === "prefix" ? "/work/project/src/" : "/work/project/src/app.py"} onChange={(e) => setDraft(e.target.value)} />
        </div>
        <fieldset className="hist-field hist-mode">
          <legend className="hud-label">Match</legend>
          <div className="chips">
            {(["exact", "prefix"] as const).map((m) => (
              <button key={m} type="button" className={`fchip${draftMode === m ? " on" : ""}`} aria-pressed={draftMode === m} onClick={() => setDraftMode(m)}>
                {m === "exact" ? "Exact path" : "Prefix"}
              </button>
            ))}
          </div>
        </fieldset>
        <div className="hist-actions">
          <button type="submit" className="act-btn" disabled={draft.trim() === ""}>Find</button>
        </div>
      </form>

      {state.status === "idle" ? <p className="muted hist-note">Enter the full path of a file, or a folder prefix, to see every session that read or wrote it.</p> : null}
      {state.status === "loading" ? <p className="muted hist-note" role="status">Searching</p> : null}
      {state.status === "error" ? <p className="err-text hist-note" role="alert">{state.message}</p> : null}
      {state.status === "ok" && state.rows.length === 0 ? <div className="panel hist-note"><p>No session touched {mode === "prefix" ? "anything under" : ""} <code>{path}</code>.</p></div> : null}
      {state.status === "ok" && state.rows.length > 0 ? (
        <section className="panel hist-table-panel" aria-label="Sessions that touched the file">
          <div className="hist-scroll" tabIndex={0} role="region" aria-label="Lookup results">
            <table className="hist-table">
              <caption className="sr-only">{plural(state.rows.length, "session")} touched {path}, latest first</caption>
              <thead>
                <tr>
                  <th scope="col">Session</th>
                  <th scope="col">{mode === "prefix" ? "Latest file" : "File"}</th>
                  <th scope="col">Latest access</th>
                  <th scope="col" className="num">Accesses</th>
                  <th scope="col">Operations</th>
                </tr>
              </thead>
              <tbody>
                {state.rows.map((r) => (
                  <tr key={`${r.machine_id}\u0000${r.session_id}`}>
                    <td className="hist-name">
                      <a className="hist-link" href={sessionHash(r.machine_id, r.session_id, back)}>{(r.session_name ?? "").trim() || r.project || r.session_id}</a>
                      {r.project ? <span className="muted hist-sub">{r.project}</span> : null}
                    </td>
                    <td className="hist-path"><MiddleText text={r.path} /></td>
                    <td className="tnum">{formatDateTime(r.t)}</td>
                    <td className="tnum num">{r.accesses}</td>
                    <td>{r.ops.map((o) => <span key={o} className={`chip op-${o}`}>{o}</span>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {state.next || state.moreError ? (
            <div className="hist-more">
              <button type="button" className="act-btn" disabled={state.more} onClick={() => { setState({ ...state, more: true, moreError: null }); fetchPage(state.next, state.rows); }}>
                {state.more ? "Loading" : "Load more"}
              </button>
              {state.moreError ? <span className="err-text" role="alert">{state.moreError}</span> : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </main>
  );
}
