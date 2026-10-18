// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HudView } from "../components/HudView";
import type { Agent, Session } from "../types";
import { ApiError, apiGet, AuthError, login } from "./api";
import { Login } from "./Login";
import { AuthContext, useApi, type ApiState } from "./useApi";
import { CallDrawer } from "../components/CallDrawer";
import { renderHook } from "@testing-library/react";

const resp = (status: number, body: unknown = {}) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

describe("login", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const submit = async (token: string): Promise<void> => {
    fireEvent.change(screen.getByLabelText("Hub token"), { target: { value: token } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  };

  it("uses a password field and posts {token} to /login with same-origin credentials", async () => {
    fetchMock.mockResolvedValue(resp(200, { ok: true }));
    const onSuccess = vi.fn();
    render(<Login onSuccess={onSuccess} />);
    expect((screen.getByLabelText("Hub token") as HTMLInputElement).type).toBe("password");
    await submit("tok-value");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/login");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual({ token: "tok-value" });
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
  });

  it("401 -> message, then a retry succeeds and reloads the route", async () => {
    fetchMock.mockResolvedValueOnce(resp(401, { detail: "unauthorized" })).mockResolvedValueOnce(resp(200, { ok: true }));
    const onSuccess = vi.fn();
    render(<Login onSuccess={onSuccess} />);
    await submit("wrong");
    expect((await screen.findByRole("alert")).textContent).toBe("Wrong token.");
    expect(onSuccess).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Hub token"), { target: { value: "right" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("429 and 413 and network failures have their own messages", async () => {
    fetchMock.mockResolvedValueOnce(resp(429));
    expect(await login("x")).toEqual({ ok: false, message: "Too many attempts, wait a minute." });
    fetchMock.mockResolvedValueOnce(resp(413));
    expect(await login("x")).toEqual({ ok: false, message: "That token is too long." });
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));
    expect(await login("x")).toMatchObject({ ok: false });
    fetchMock.mockResolvedValueOnce(resp(500));
    expect(await login("x")).toEqual({ ok: false, message: "Sign-in failed (500)." });
  });

  it("shows the rate-limit message in the form and never touches web storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    fetchMock.mockResolvedValue(resp(429));
    render(<Login onSuccess={() => {}} />);
    await submit("whatever");
    expect((await screen.findByRole("alert")).textContent).toBe("Too many attempts, wait a minute.");
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it("apiGet sends credentials and maps 401 to AuthError and other failures to ApiError", async () => {
    fetchMock.mockResolvedValueOnce(resp(401));
    await expect(apiGet("/machines")).rejects.toBeInstanceOf(AuthError);
    expect((fetchMock.mock.calls[0] as [string, RequestInit])[1].credentials).toBe("same-origin");
    fetchMock.mockResolvedValueOnce(resp(404));
    await expect(apiGet("/sessions/a/b")).rejects.toMatchObject({ status: 404, message: "Not found" });
    fetchMock.mockResolvedValueOnce(resp(400, { detail: "invalid cursor" }));
    await expect(apiGet("/sessions", new URLSearchParams("cursor=x"))).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/history/sessions?cursor=x");
  });
});

const T0 = 1_700_000_000;
const agent = (over: Partial<Agent> = {}): Agent => ({
  id: "main", label: "Main", agent_type: null, status: "running", task: "Fix the bug", result: "", started: T0, ended: null,
  context_tokens: 1000, context_window: 200000, model: "m", tool_counts: { Read: 1 }, errors: 0, total_calls: 1,
  calls: [{ id: "c1", agent_id: "main", tool: "Read", summary: "a.py", status: "running", started: T0 + 1, ended: null, duration_ms: null, subagent_id: null }],
  ...over,
});
// A replay whose end was never recorded: still "active" with a running call.
const unfinished: Session = {
  id: "s1", title: "proj", name: "Parser fixes", cwd: "/p", model: "m", status: "active", started: T0, ended: null, last_event: T0 + 30,
  compactions: 0, agents: [agent()], activity: [],
};

describe("HudView in history mode", () => {
  beforeEach(() => {
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    };
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    (globalThis as { CSS?: unknown }).CSS = { escape: (s: string) => s.replace(/[^\w-]/g, "\\$&") };
    vi.stubGlobal("fetch", vi.fn(async () => resp(200, { id: "c1", tool: "Read", status: "ok", started: 1, ended: 2, duration_ms: 1000, input: { file_path: "/p/a.py" }, output: "ok", error: null })));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  const hud = (live: boolean, extra: Partial<Parameters<typeof HudView>[0]> = {}) => (
    <HudView
      session={unfinished}
      live={live}
      callDetailUrl={(s, c) => `/api/history/sessions/m/${s}/calls/${c}`}
      renderHeader={(actions) => <header>{actions}</header>}
      {...extra}
    />
  );

  it("renders the session read-only: no Follow live control, nothing counted as running", () => {
    render(hud(false));
    expect(screen.getByRole("region", { name: "Timeline" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Follow live" })).toBeNull();
    expect(screen.getByRole("button", { name: "Fit" })).toBeTruthy();
    // frozen as ended: the lane shows the call as finished/unknown, not as live "running" time
    expect(document.querySelector(".hud-static")).not.toBeNull();
    expect(screen.getByText("Ended")).toBeTruthy();
  });

  it("the live dashboard keeps its Follow live control", () => {
    render(hud(true));
    expect(screen.getByRole("button", { name: "Follow live" })).toBeTruthy();
    expect(document.querySelector(".hud-static")).toBeNull();
  });

  it("opens a call from the Files tab hook with the history detail URL", async () => {
    let api: { openCall: (id: string, tool: string) => void } | null = null;
    render(hud(false, { renderTab: (a) => { api = a; return <p>files tab</p>; } }));
    expect(screen.getByText("files tab")).toBeTruthy();
    expect(document.querySelector(".body")).toBeNull();
    api!.openCall("c1", "Read");
    await screen.findByRole("dialog");
    await waitFor(() => expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toBe("/api/history/sessions/m/s1/calls/c1"));
  });
});

describe("useApi", () => {
  afterEach(() => cleanup());

  it("never shows the previous key's data in the first render after the key changes", async () => {
    const renders: string[] = [];
    const { rerender, result } = renderHook(
      ({ k }: { k: string }) => {
        const st: ApiState<string> = useApi(k, async () => `data-${k}`);
        renders.push(`${k}:${st.status === "ok" ? st.data : st.status}`);
        return st;
      },
      { initialProps: { k: "a" } },
    );
    await waitFor(() => expect(result.current).toEqual({ status: "ok", data: "data-a" }));
    renders.length = 0;
    rerender({ k: "b" });
    expect(renders[0]).toBe("b:loading");
    await waitFor(() => expect(result.current).toEqual({ status: "ok", data: "data-b" }));
    expect(renders.some((r) => r === "b:data-a")).toBe(false);
  });

  it("a 401 asks for the login view instead of showing an error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => resp(401)));
    const need = vi.fn();
    const wrapper = ({ children }: { children: React.ReactNode }) => <AuthContext.Provider value={need}>{children}</AuthContext.Provider>;
    const { result } = renderHook(() => useApi("x", (sig) => apiGet("/machines", undefined, sig)), { wrapper });
    await waitFor(() => expect(need).toHaveBeenCalledOnce());
    expect(result.current.status).toBe("loading");
    vi.unstubAllGlobals();
  });
});

describe("CallDrawer 401", () => {
  beforeEach(() => {
    (globalThis as { CSS?: unknown }).CSS = { escape: (x: string) => x };
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });
  const drawer = (extra: Partial<Parameters<typeof CallDrawer>[0]> = {}) => (
    <CallDrawer sessionId="s" callId="c" tool="Read" urlFor={() => "/x"} status="ok" onClose={() => {}} {...extra} />
  );

  it("calls onUnauthorized when provided (history: back to the login view)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => resp(401)));
    const onUnauthorized = vi.fn();
    render(drawer({ onUnauthorized }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
  });

  it("keeps the live behaviour without it: an inline message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => resp(401)));
    render(drawer());
    expect((await screen.findByText(/Signed out/)).textContent).toContain("Reload");
  });
});
