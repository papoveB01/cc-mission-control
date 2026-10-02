import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { formatClockSeconds, formatDuration } from "../format";
import type { CallStatus, ToolCallDetail } from "../types";
import { Elapsed } from "./Elapsed";

type Load =
  | { kind: "loading" }
  | { kind: "ok"; detail: ToolCallDetail }
  | { kind: "gone" }
  | { kind: "error"; message: string };

interface Props {
  sessionId: string;
  callId: string;
  tool: string;
  /** Live status of the call from the session state (drives refetch). */
  status: CallStatus | null;
  onClose: () => void;
}

function narrowDetail(v: unknown): ToolCallDetail | null {
  if (typeof v !== "object" || v === null) return null;
  const r = v as Record<string, unknown>;
  if (typeof r.id !== "string" || typeof r.tool !== "string") return null;
  return v as ToolCallDetail;
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function CallDrawer({ sessionId, callId, tool, status, onClose }: Props) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const panel = useRef<HTMLDivElement>(null);
  const returnTo = useRef<Element | null>(document.activeElement);

  useEffect(() => {
    panel.current?.focus();
    const target = returnTo.current;
    return () => {
      if (target instanceof HTMLElement && target.isConnected) target.focus();
    };
  }, []);

  useEffect(() => {
    const ctl = new AbortController();
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}/calls/${encodeURIComponent(callId)}`, { signal: ctl.signal })
      .then(async (res) => {
        if (res.status === 404) return setLoad({ kind: "gone" });
        if (!res.ok) return setLoad({ kind: "error", message: `Request failed (${res.status})` });
        const detail = narrowDetail(await res.json());
        setLoad(detail ? { kind: "ok", detail } : { kind: "error", message: "Unexpected response" });
      })
      .catch((err: unknown) => {
        if (ctl.signal.aborted) return;
        setLoad({ kind: "error", message: err instanceof Error ? err.message : "Request failed" });
      });
    return () => ctl.abort();
  }, [sessionId, callId, status]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const trap = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== "Tab" || !panel.current) return;
    const nodes = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  };

  const detail = load.kind === "ok" ? load.detail : null;
  const shownStatus = status ?? detail?.status ?? null;

  return (
    <div className="drawer-root">
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        tabIndex={-1}
        ref={panel}
        onKeyDown={trap}
      >
        <div className="drawer-head">
          <h2 id="drawer-title">{detail?.tool ?? tool}</h2>
          {shownStatus ? <span className={`chip st-chip-${shownStatus}`}>{shownStatus === "ok" ? "succeeded" : shownStatus === "error" ? "failed" : "running"}</span> : null}
          <button type="button" className="close-btn" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="drawer-body">
          {load.kind === "gone" ? <p className="muted">This call is no longer in memory.</p> : null}
          {load.kind === "error" ? <p className="err-text">{load.message}</p> : null}
          {load.kind === "loading" ? <p className="muted">Loading</p> : null}
          {detail ? (
            <>
              <dl className="meta">
                <dt>Started</dt>
                <dd className="tnum">{formatClockSeconds(detail.started)}</dd>
                <dt>Ended</dt>
                <dd className="tnum">{detail.ended !== null ? formatClockSeconds(detail.ended) : "still running"}</dd>
                <dt>Duration</dt>
                <dd className="tnum">
                  {detail.duration_ms !== null ? formatDuration(detail.duration_ms) : <Elapsed start={detail.started} end={null} />}
                </dd>
              </dl>
              <h3 className="label">Input</h3>
              <pre className="code" tabIndex={0}>{JSON.stringify(detail.input, null, 2)}</pre>
              <h3 className="label">Output</h3>
              <pre className="code" tabIndex={0}>{detail.output || "(empty)"}</pre>
              {detail.error ? (
                <>
                  <h3 className="label">Error</h3>
                  <pre className="code code-err" tabIndex={0}>{detail.error}</pre>
                </>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
