import { FormEvent, useState } from "react";
import { api, ApiError, User } from "../api";

export function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.post<User>("/api/login", { name, password }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not reach the desk");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form onSubmit={submit} className="card login-card">
        <h1>Trading Desk</h1>
        <p className="muted">Sign in to watch the book and talk to the head trader.</p>
        <label>
          Name
          <input id="login-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoFocus />
        </label>
        <label>
          Password
          <input
            id="login-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button type="submit" disabled={busy || !name || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
