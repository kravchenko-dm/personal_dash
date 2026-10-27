import { useState, type FormEvent } from "react";
import { post, setToken } from "../lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await post<{ token: string }>("/api/auth/login", { password });
      setToken(token);
      onLogin();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="stack">
        <h1>Personal Dash</h1>
        <p className="muted">Введите пароль дашборда</p>
        <input
          className="input"
          type="password"
          autoFocus
          placeholder="Пароль"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <p className="small" style={{ color: "var(--danger)" }}>{error}</p>}
        <button className="btn primary" style={{ width: "100%" }} disabled={busy || !password}>
          Войти
        </button>
      </form>
    </div>
  );
}
