import { memo } from "react";
import { toolLabel } from "../format";
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
  /** Dimmed by the active feed filters (not hidden). */
  dim?: boolean;
  onOpen: (call: ToolCall, trigger: Element) => void;
  onJump: (agentId: string, trigger: Element) => void;
}

export const ToolCallRow = memo(function ToolCallRow({ call, spawnLabel, stale, cap, dim = false, onOpen, onJump }: Props) {
  const mono = !PROSE_TOOLS.has(call.tool);
  const running = call.status === "running";
  const mode = running ? runningCallElapsed(stale, cap) : null;
  return (
    <li className={`call${dim ? " dim" : ""}`}>
      <button type="button" className="call-main" onClick={(e) => onOpen(call, e.currentTarget)} data-call-id={call.id}>
        <span className={`dot dot-${running && stale ? "stale" : call.status}`} aria-hidden="true" />
        <span className="sr-only">{running && stale ? "interrupted" : STATUS_TEXT[call.status]}: </span>
        <span className="call-tool" title={call.tool}>{toolLabel(call.tool)}</span>
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
          onClick={(e) => onJump(call.subagent_id as string, e.currentTarget)}
          aria-label={`Go to ${spawnLabel} lane`}
        >
          {"\u2192 "}
          {spawnLabel}
        </button>
      ) : null}
    </li>
  );
});
