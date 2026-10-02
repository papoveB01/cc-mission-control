import { useEffect, useReducer, useState } from "react";
import { initialState, parseMessage, reduce, type MissionState, type ServerMessage } from "./store";
import type { ConnectionState } from "./types";

export const BACKOFF_MIN_MS = 500;
export const BACKOFF_MAX_MS = 5000;
export const OFFLINE_AFTER_FAILURES = 3;
const PING_MS = 20_000;

interface Action {
  msg: ServerMessage | null;
  now: number;
}

const reducer = (state: MissionState, a: Action): MissionState => reduce(state, a.msg, a.now);

/** 0.5 s, 1 s, 2 s, 4 s, 5 s, 5 s ... */
export function backoffDelay(failures: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.max(0, failures));
}

export function connectionAfterFailure(consecutiveFailures: number): ConnectionState {
  return consecutiveFailures >= OFFLINE_AFTER_FAILURES ? "offline" : "reconnecting";
}

export function useMissionSocket(): { state: MissionState; connection: ConnectionState } {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [connection, setConnection] = useState<ConnectionState>("reconnecting");

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let ping: ReturnType<typeof setInterval> | null = null;
    let failures = 0;

    const connect = (): void => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const sock = new WebSocket(`${scheme}://${location.host}/ws`);
      ws = sock;
      sock.onopen = () => {
        failures = 0;
        setConnection("live");
        ping = setInterval(() => {
          if (sock.readyState === WebSocket.OPEN) sock.send("ping");
        }, PING_MS);
      };
      sock.onmessage = (ev: MessageEvent) => {
        dispatch({ msg: parseMessage(ev.data), now: Date.now() });
      };
      sock.onclose = () => {
        if (ping !== null) clearInterval(ping);
        ping = null;
        if (closed) return;
        const delay = backoffDelay(failures);
        failures += 1;
        setConnection(connectionAfterFailure(failures));
        retry = setTimeout(connect, delay);
      };
      sock.onerror = () => sock.close();
    };

    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      if (ping !== null) clearInterval(ping);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, []);

  return { state, connection };
}
