import type { Session } from "../types";

/** One row of GET /api/history/sessions. */
export interface SessionRow {
  machine_id: string;
  session_id: string;
  user: string;
  host: string;
  name: string | null;
  name_source: string | null;
  project: string;
  cwd: string;
  model: string | null;
  status: string;
  started: number;
  ended: number | null;
  last_event: number;
  agents: number;
  tool_calls: number;
  errors: number;
  compactions: number;
  tokens_in: number;
  tokens_cache_create: number;
  tokens_cache_read: number;
  tokens_out: number;
  peak_context: number;
}

export interface SessionPage {
  sessions: SessionRow[];
  next_cursor: string | null;
}

export interface MachineRow {
  machine_id: string;
  user: string | null;
  host: string | null;
  first_seen: number;
  last_seen: number;
  sessions: number;
}

export type FileOp = "read" | "write" | "edit" | "search";
export const FILE_OPS: readonly FileOp[] = ["read", "write", "edit", "search"];

/** One row of GET .../sessions/{m}/{s}/files. */
export interface FileAccess {
  agent_id: string;
  call_id: string;
  path: string;
  op: string;
  tool: string;
  status: string;
  t: number;
}

export interface PermissionRow {
  agent_id: string;
  call_id: string;
  tool: string;
  kind: string;
  text: string;
  t: number;
}

export interface UsageCounts {
  input: number;
  cache_creation: number;
  cache_read: number;
  output: number;
  messages: number;
}

/** A replayed session: the live `Session` plus who ran it and token usage per agent. */
export interface HistorySession extends Session {
  machine_id: string;
  user: string;
  host: string;
  usage: Record<string, UsageCounts>;
}

/** One row of GET /api/history/files: a session's latest access, count and ops. */
export interface FileHit {
  machine_id: string;
  session_id: string;
  session_name: string | null;
  project: string | null;
  agent_id: string;
  call_id: string;
  path: string;
  op: string;
  tool: string;
  status: string;
  t: number;
  accesses: number;
  ops: string[];
}

export interface FilePage {
  files: FileHit[];
  next_cursor: string | null;
}
