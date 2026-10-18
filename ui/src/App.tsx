import { useCallback, useMemo, useState } from "react";
import { liveCallUrl } from "./components/CallDrawer";
import { Header } from "./components/Header";
import { HudView } from "./components/HudView";
import { UpdateBanner } from "./components/UpdateBanner";
import { orderSessions, resolveSelected } from "./store";
import { useMissionSocket } from "./useMissionSocket";

/** The live dashboard: the socket feeds the shared HUD view. */
export function App() {
  const { state, connection } = useMissionSocket();
  const [explicit, setExplicit] = useState<string | null>(null);
  const onSelectSession = useCallback((id: string) => setExplicit(id), []);

  const orderKey = [...state.sessions.values()].map((s) => `${s.id}\u0000${s.status}\u0000${s.title}\u0000${s.name ?? ""}\u0000${s.name_source ?? ""}\u0000${s.started}\u0000${s.cwd}`).join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- Header only reads the fields in orderKey
  const ordered = useMemo(() => orderSessions(state.sessions.values()), [orderKey]);
  const selectedId = resolveSelected(state.sessions, explicit);
  const session = selectedId ? (state.sessions.get(selectedId) ?? null) : null;

  return (
    <>
      <UpdateBanner serverVersion={state.version} />
      <HudView
        session={session}
        live
        callDetailUrl={liveCallUrl}
        sessions={ordered}
        onSelectSession={onSelectSession}
        history={state.history}
        epoch={state.snapshots}
        renderHeader={(actions) => <Header sessions={ordered} selectedId={selectedId} connection={connection} onSelect={onSelectSession} actions={actions} />}
      />
    </>
  );
}
