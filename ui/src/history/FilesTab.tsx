import { useMemo, useState } from "react";
import { MiddleText } from "../components/MiddleText";
import type { HudApi } from "../components/HudView";
import { formatClockSeconds, plural } from "../format";
import { agentName, laneNames } from "../store";
import type { Session } from "../types";
import { apiGet, sessionPath } from "./api";
import { defaultFileView, nextSort, viewFiles, type FileView, type SortKey } from "./filesTable";
import { filesHash, sessionHash } from "./route";
import { FILE_OPS, type FileAccess, type PermissionRow } from "./types";
import { useApi } from "./useApi";

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "path", label: "Path" },
  { key: "op", label: "Op" },
  { key: "agent", label: "Agent" },
  { key: "time", label: "Time" },
  { key: "status", label: "Status" },
];

const STATUS_TEXT: Record<string, string> = { ok: "succeeded", error: "failed", running: "running" };

function SortHead({ col, view, onSort }: { col: { key: SortKey; label: string }; view: FileView; onSort: (k: SortKey) => void }) {
  const active = view.sort === col.key;
  return (
    <th scope="col" aria-sort={active ? (view.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" className="hist-sort" onClick={() => onSort(col.key)}>
        {col.label}
        <span aria-hidden="true" className="hist-arrow">{active ? (view.dir === "asc" ? " ^" : " v") : ""}</span>
      </button>
    </th>
  );
}

function Permissions({ rows, label }: { rows: PermissionRow[]; label: (id: string) => string }) {
  return (
    <section className="panel hist-perms" aria-label="Permissions">
      <header className="panel-head"><h2 className="hud-title">Permissions</h2></header>
      {rows.length === 0 ? (
        <p className="muted hist-pad">No permission prompts or denials were recorded.</p>
      ) : (
        <ul className="hist-perm-list">
          {rows.map((p, i) => (
            <li key={`${p.call_id}-${i}`}>
              <span className={`chip${p.kind === "denied" ? " chip-err" : ""}`}>{p.kind === "denied" ? "denied" : "prompt"}</span>
              <span className="tnum muted">{formatClockSeconds(p.t)}</span>
              <span className="muted">{label(p.agent_id)}</span>
              {p.tool ? <span className="mono">{p.tool}</span> : null}
              <span className="hist-perm-text">{p.text || "(no detail)"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Files read, written, edited or searched in one session. A row opens its call. */
/** `back` is where this session was opened from; a path link returns to this tab, which returns there. */
export function FilesTab({ machine, sessionId, session, hud, back }: { machine: string; sessionId: string; session: Session; hud: HudApi; back: string }) {
  const here = sessionHash(machine, sessionId, back, "files").slice(1);
  const files = useApi<{ files: FileAccess[] }>(`files:${machine}/${sessionId}`, (s) => apiGet(`${sessionPath(machine, sessionId)}/files`, undefined, s));
  const perms = useApi<{ permissions: PermissionRow[] }>(`perms:${machine}/${sessionId}`, (s) => apiGet(`${sessionPath(machine, sessionId)}/permissions`, undefined, s));
  const [view, setView] = useState<FileView>(defaultFileView);
  const names = useMemo(() => laneNames(session.agents), [session.agents]);
  const label = (id: string): string => agentName(names, id);
  const rows = useMemo(() => (files.status === "ok" ? viewFiles(files.data.files, view, (id) => agentName(names, id)) : []), [files, view, names]);
  const total = files.status === "ok" ? files.data.files.length : 0;

  return (
    <div className="hist-tab">
      <section className="panel" aria-label="Files">
        <header className="panel-head">
          <h2 className="hud-title">Files</h2>
          {files.status === "ok" ? <span className="muted tnum">{rows.length === total ? plural(total, "access") : `${rows.length} of ${total}`}</span> : null}
        </header>
        <div className="hist-toolbar">
          <div className="chips" role="group" aria-label="Filter by operation">
            {(["all", ...FILE_OPS] as const).map((op) => (
              <button key={op} type="button" className={`fchip${view.op === op ? " on" : ""}`} aria-pressed={view.op === op} onClick={() => setView({ ...view, op })}>
                {op}
              </button>
            ))}
          </div>
          <input
            className="m-input"
            type="search"
            value={view.q}
            placeholder="Filter path"
            aria-label="Filter files by path"
            onChange={(e) => setView({ ...view, q: e.target.value })}
          />
        </div>
        {files.status === "loading" ? <p className="muted hist-pad" role="status">Loading files</p> : null}
        {files.status === "error" ? <p className="err-text hist-pad" role="alert">{files.message}</p> : null}
        {files.status === "ok" && total === 0 ? <p className="muted hist-pad">No file access was recorded in this session.</p> : null}
        {files.status === "ok" && total > 0 && rows.length === 0 ? <p className="muted hist-pad">No files match the filters.</p> : null}
        {rows.length > 0 ? (
          <div className="hist-scroll" tabIndex={0} role="region" aria-label="Files table">
            <table className="hist-table hist-files">
              <caption className="sr-only">Files touched in this session. Activate a time to open its call.</caption>
              <colgroup>
                <col />
                <col className="c-op" />
                <col className="c-agent" />
                <col className="c-time" />
                <col className="c-status" />
              </colgroup>
              <thead>
                <tr>{COLUMNS.map((c) => <SortHead key={c.key} col={c} view={view} onSort={(k) => setView(nextSort(view, k))} />)}</tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.call_id}\u0000${r.path}`} className="hist-click" onClick={(e) => { if (!(e.target as Element).closest("a, button")) hud.openCall(r.call_id, r.tool, e.currentTarget); }}>
                    <td className="hist-path"><a href={filesHash(r.path, "exact", here)} className="hist-link" title={`Find sessions that touched ${r.path}`}><MiddleText text={r.path} /></a></td>
                    <td><span className={`chip op-${r.op}`}>{r.op}</span></td>
                    <td><MiddleText text={label(r.agent_id)} /></td>
                    <td className="tnum">
                      <button type="button" className="link-btn inline" aria-label={`Open ${r.tool} call at ${formatClockSeconds(r.t)}`} onClick={(e) => hud.openCall(r.call_id, r.tool, e.currentTarget)}>
                        {formatClockSeconds(r.t)}
                      </button>
                    </td>
                    <td><span className={`chip st-chip-${r.status}`}>{STATUS_TEXT[r.status] ?? r.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      {perms.status === "ok" ? <Permissions rows={perms.data.permissions} label={label} /> : null}
      {perms.status === "error" ? <p className="err-text" role="alert">Permissions: {perms.message}</p> : null}
    </div>
  );
}
