import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { FileLookup } from "./FileLookup";
import { LIST_HASH } from "./route";
import { Login } from "./Login";
import { useRoute } from "./router";
import { SessionList } from "./SessionList";
import { SessionPage } from "./SessionPage";
import { AuthContext } from "./useApi";

const TITLES: Record<string, string> = {
  list: "Sessions · History",
  files: "Files · History",
  session: "Session · History",
  notfound: "Not found · History",
};

function Shell({ children }: { children: ReactNode }) {
  return (
    <>
      <header className="header hist-header">
        <a className="brand hist-brand" href={LIST_HASH}><span className="brand-mark" aria-hidden="true" />Mission Control</a>
        <nav className="hist-nav" aria-label="History">
          <a href={LIST_HASH}>Sessions</a>
          <a href="#/files">File lookup</a>
        </nav>
      </header>
      {children}
    </>
  );
}

/** The history page: hash routes, a login gate on 401. */
export function HistoryApp() {
  const route = useRoute();
  const [needLogin, setNeedLogin] = useState(false);
  const requireLogin = useCallback(() => setNeedLogin(true), []);

  // The tab title follows the route (a session page sets its own once loaded). On a route change,
  // not on first load, focus moves to the new page so keyboard and screen reader users start there.
  const first = useRef(true);
  useEffect(() => {
    document.title = needLogin ? "Sign in · History" : (TITLES[route.name] ?? "History");
    if (first.current) {
      first.current = false;
      return;
    }
    if (!needLogin && route.name !== "session") document.getElementById("main")?.focus({ preventScroll: true });
  }, [route.name, needLogin]);

  if (needLogin) return <Login />;

  let page: ReactNode;
  switch (route.name) {
    case "list":
      page = <Shell><SessionList filters={route.filters} /></Shell>;
      break;
    case "files":
      page = <Shell><FileLookup path={route.path} mode={route.mode} back={route.back} /></Shell>;
      break;
    case "session":
      page = <SessionPage machine={route.machine} sessionId={route.session} tab={route.tab} back={route.back} />;
      break;
    case "notfound":
      page = (
        <Shell>
          <main className="hist-main" id="main" tabIndex={-1}>
            <div className="panel hist-note"><p>That page does not exist.</p><a href={LIST_HASH}>Back to sessions</a></div>
          </main>
        </Shell>
      );
      break;
  }
  return (
    <AuthContext.Provider value={requireLogin}>
      <button type="button" className="skip-link" onClick={() => (document.getElementById("main") ?? document.getElementById("session-panel"))?.focus()}>
        Skip to content
      </button>
      {page}
    </AuthContext.Provider>
  );
}
