export type AgentStatus = "idle" | "running" | "waiting" | "done" | "error";
export type CallStatus = "running" | "ok" | "error";
export type ActivityStatus = "info" | "running" | "ok" | "error";

export interface ToolCall {
  id: string;
  agent_id: string;
  tool: string;
  summary: string;
  status: CallStatus;
  started: number;
  ended: number | null;
  duration_ms: number | null;
  subagent_id: string | null;
}

export interface ToolCallDetail extends ToolCall {
  input: unknown;
  output: string;
  error: string | null;
}

export interface Agent {
  id: string;
  label: string;
  agent_type: string | null;
  status: AgentStatus;
  task: string;
  result: string;
  started: number;
  ended: number | null;
  context_tokens: number | null;
  context_window: number | null;
  model: string | null;
  tool_counts: Record<string, number>;
  errors: number;
  total_calls: number;
  calls: ToolCall[];
}

export interface Activity {
  t: number;
  agent_id: string;
  kind: string;
  text: string;
  status: ActivityStatus;
}

export type NameSource = "custom" | "generated" | "folder";

export interface Session {
  id: string;
  /** Project folder basename. */
  title: string;
  /** Display name (custom title, else generated title, else folder). Absent on older servers. */
  name?: string;
  name_source?: NameSource;
  cwd: string;
  model: string | null;
  status: "active" | "ended";
  started: number;
  ended: number | null;
  last_event: number;
  compactions: number;
  agents: Agent[];
  activity: Activity[];
}

export type ConnectionState = "live" | "reconnecting" | "offline";
