import { useEffect, useRef, useState, type FormEvent } from "react";
import { login } from "./api";

/**
 * Asks for the hub token and POSTs it to /login. The HttpOnly cookie in the response is the
 * session: the token lives only in this input, never in web storage.
 */
export function Login({ onSuccess = () => window.location.reload() }: { onSuccess?: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);

  const submit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();
    if (busy || token === "") return;
    setBusy(true);
    setError(null);
    const result = await login(token);
    setBusy(false);
    if (result.ok) {
      setToken("");
      onSuccess();
    } else {
      setError(result.message);
      input.current?.focus();
    }
  };

  return (
    <main className="hist-login" id="main">
      <form className="panel hist-login-card" onSubmit={(e) => void submit(e)}>
        <h1 className="brand"><span className="brand-mark" aria-hidden="true" />Mission Control</h1>
        <h2 className="hud-title">Session history</h2>
        <p className="muted">Enter the hub token to continue.</p>
        {/* Password managers and browsers expect a username beside a password field. */}
        <input type="text" name="username" value="hub" autoComplete="username" readOnly hidden />
        <label className="hud-label" htmlFor="hub-token">Hub token</label>
        <input
          id="hub-token"
          ref={input}
          className="m-input"
          type="password"
          autoComplete="current-password"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? "login-error" : undefined}
        />
        <button type="submit" className="act-btn" disabled={busy || token === ""}>
          {busy ? "Signing in" : "Sign in"}
        </button>
        <p id="login-error" className="err-text" role="alert">{error}</p>
      </form>
    </main>
  );
}
