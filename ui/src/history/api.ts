/** Same-origin calls to the hub. The HttpOnly session cookie is the credential; no token is kept here. */

export class AuthError extends Error {
  constructor() {
    super("Sign in required");
    this.name = "AuthError";
  }
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const API = "/api/history";
const enc = encodeURIComponent;

/** Full redacted detail of one call (the drawer). */
export const callDetailUrl = (machine: string, session: string, callId: string): string =>
  `${API}/sessions/${enc(machine)}/${enc(session)}/calls/${enc(callId)}`;

export const sessionPath = (machine: string, session: string): string => `/sessions/${enc(machine)}/${enc(session)}`;

export async function apiGet<T>(path: string, params?: URLSearchParams, signal?: AbortSignal): Promise<T> {
  const qs = params?.toString();
  const res = await fetch(`${API}${path}${qs ? `?${qs}` : ""}`, { credentials: "same-origin", signal, headers: { Accept: "application/json" } });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let detail = "";
    try {
      const body: unknown = await res.json();
      const d = typeof body === "object" && body !== null ? (body as { detail?: unknown }).detail : undefined;
      if (typeof d === "string") detail = d;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, res.status === 404 ? "Not found" : detail || `Request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

export type LoginResult = { ok: true } | { ok: false; message: string };

/** POST /login {token}. The response sets the HttpOnly cookie; the token is not retained anywhere. */
export async function login(token: string): Promise<LoginResult> {
  let res: Response;
  try {
    res = await fetch("/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
  } catch {
    return { ok: false, message: "Could not reach the hub." };
  }
  if (res.ok) return { ok: true };
  if (res.status === 401) return { ok: false, message: "Wrong token." };
  if (res.status === 429) return { ok: false, message: "Too many attempts, wait a minute." };
  if (res.status === 413) return { ok: false, message: "That token is too long." };
  return { ok: false, message: `Sign-in failed (${res.status}).` };
}
