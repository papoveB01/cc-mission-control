import { memo } from "react";
import { runningCallElapsed } from "../store";
import type { ToolCall } from "../types";
import { Elapsed } from "./Elapsed";

const PROSE_TOOLS = new Set(["Agent", "Task", "TodoWrite", "WebSearch", "AskUserQuestion", "Skill"]);

const STATUS_TEXT = { running: "running", ok: "succeeded", error: "failed" } as const;

interface Props {
  call: ToolCall;
  spawnLabel: string | null;
  /** Running call whose agent finished or session ended. */
  stale: boolean;
  /** End time to clamp a stale running call to. */
  cap: number | null;
  onOpen: (call: ToolCall) => void;
  onJump: (agentId: string) => void;
}

export const ToolCallRow = memo(function ToolCallRow({ call, spawnLabel, stale, cap, onOpen, onJump }: Props) {
  const mono = !PROSE_TOOLS.has(call.tool);
  const running = call.status === "running";
  const mode = running ? runningCallElapsed(stale, cap) : null;
  return (
    <li className="call">
      <button type="button" className="call-main" onClick={() => onOpen(call)} data-call-id={call.id}>
        <span className={`dot dot-${running && stale ? "stale" : call.status}`} aria-hidden="true" />
        <span className="sr-only">{running && stale ? "interrupted" : STATUS_TEXT[call.status]}: </span>
        <span className="call-tool">{call.tool}</span>
        <span className={`call-summary${mono ? " mono" : ""}`}>{call.summary}</span>
        <span className="call-time">
          {mode === null ? (
            <Elapsed start={0} end={(call.duration_ms ?? Math.max(0, ((call.ended ?? call.started) - call.started) * 1000)) / 1000} />
          ) : mode.kind === "live" ? (
            <Elapsed start={call.started} end={null} />
          ) : mode.kind === "fixed" ? (
            <Elapsed start={call.started} end={Math.max(mode.end, call.started)} />
          ) : (
            "unknown"
          )}
        </span>
      </button>
      {call.subagent_id && spawnLabel ? (
        <button
          type="button"
          className="spawn-link"
          onClick={() => onJump(call.subagent_id as string)}
          aria-label={`Go to ${spawnLabel} lane`}
        >
          {"\u2192 "}
          {spawnLabel}
        </button>
      ) : null}
    </li>
  );
});
