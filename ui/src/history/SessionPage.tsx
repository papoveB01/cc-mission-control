import { useCallback, useContext, useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { HudView } from "../components/HudView";
import { formatDuration } from "../format";
import { sessionName } from "../store";
import { apiGet, callDetailUrl, sessionPath } from "./api";
import { FilesTab } from "./FilesTab";
import { formatDateTime, machineLabel } from "./fmt";
import { navigate } from "./router";
import { LIST_HASH, buildHash, sanitizeBack, TABS, type SessionTab } from "./route";
import type { HistorySession } from "./types";
import { UsageTab } from "./UsageTab";
import { AuthContext, useApi } from "./useApi";

const TAB_LABEL: Record<SessionTab, string> = { hud: "Overview", files: "Files", usage: "Usage" };

interface HeaderProps {
  session: HistorySession | null;
  machine: string;
  sessionId: string;
  tab: SessionTab;
  back: string;
  actions: ReactNode;
}

function SessionHeader({ session, machine, sessionId, tab, back, actions }: HeaderProps) {
  const list = useRef<HTMLDivElement>(null);
  const backHref = back ? `#${back}` : LIST_HASH;
  const go = (t: SessionTab, replace = false): void => navigate(buildHash({ name: "session", machine, session: sessionId, tab: t, back }), replace);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    const i = TABS.indexOf(tab);
    let next = i;
    if (e.key === "ArrowRight") next = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    else return;
    e.preventDefault();
    const t = TABS[next] as SessionTab;
    go(t, true);
    requestAnimationFrame(() => list.current?.querySelector<HTMLElement>(`#vtab-${t}`)?.focus());
  };
  const name = session ? sessionName(session) : sessionId;
  const end = session ? (session.ended ?? session.last_event) : null;
  return (
    <header className="header hist-header">
      <a className="brand hist-brand" href={LIST_HASH}><span className="brand-mark" aria-hidden="true" />Mission Control</a>
      <a className="hist-back" href={backHref}>Back to {back.startsWith("/files") ? "file lookup" : "sessions"}</a>
      <div className="hist-title">
        <h1 className="hist-session-name" title={name}>{name}</h1>
        {session ? (
          <p className="muted hist-meta">
            <span title={session.cwd}>{session.title}</span>
            {" · "}
            <span title={session.machine_id}>{machineLabel(session.user, session.host, session.machine_id)}</span>
            {" · "}
            <span className="tnum">{formatDateTime(session.started)} to {end ? formatDateTime(end) : "?"}</span>
            {" · "}
            <span className="tnum">{formatDuration(Math.max(0, (end ?? session.started) - session.started) * 1000)}</span>
            {session.status === "ended" ? null : " · no end recorded"}
          </p>
        ) : null}
      </div>
      <div className="tabs hist-tabs" role="tablist" aria-label="Session views" ref={list} onKeyDown={onKeyDown}>
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            id={`vtab-${t}`}
            aria-selected={t === tab}
            aria-controls="session-panel"
            tabIndex={t === tab ? 0 : -1}
            className="tab"
            onClick={() => go(t)}
          >
            <span className="tab-title">{TAB_LABEL[t]}</span>
          </button>
        ))}
      </div>
      <div className="header-actions">{actions}</div>
    </header>
  );
}

function Note({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <main className="hist-main" id="main" tabIndex={-1}>
      <div className="panel hist-note">
        <h1 className="hud-title">{title}</h1>
        {children}
        <a href={LIST_HASH}>Back to sessions</a>
      </div>
    </main>
  );
}

export function SessionPage({ machine, sessionId, tab, back }: { machine: string; sessionId: string; tab: SessionTab; back: string }) {
  const state = useApi<HistorySession>(`${machine}/${sessionId}`, (s) => apiGet(sessionPath(machine, sessionId), undefined, s));
  const detailUrl = useCallback((sid: string, callId: string) => callDetailUrl(machine, sid, callId), [machine]);
  const safeBack = sanitizeBack(back);
  const requireLogin = useContext(AuthContext);
  const ready = state.status === "ok";
  // A new page: move focus to its main region once the session has rendered.
  useEffect(() => {
    if (ready) document.getElementById("session-panel")?.focus({ preventScroll: true });
  }, [ready, machine, sessionId]);

  if (state.status === "loading") return <Note title="Loading session"><p className="muted" role="status">Loading</p></Note>;
  if (state.status === "error") return <Note title="Session not available"><p className="err-text" role="alert">{state.message}</p></Note>;
  const session = state.data;

  return (
    <HudView
      key={`${machine}/${sessionId}`}
      session={session}
      live={false}
      callDetailUrl={detailUrl}
      tabLabelId={`vtab-${tab}`}
      titleSuffix="History"
      onUnauthorized={requireLogin}
      renderHeader={(actions) => <SessionHeader session={session} machine={machine} sessionId={sessionId} tab={tab} back={safeBack} actions={actions} />}
      renderTab={(hud) =>
        tab === "files" ? (
          <FilesTab machine={machine} sessionId={sessionId} session={session} hud={hud} back={safeBack} />
        ) : tab === "usage" ? (
          <UsageTab session={session} />
        ) : null
      }
    />
  );
}
