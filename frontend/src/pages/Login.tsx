import { useEffect, useState, type FormEvent } from "react";
import { clearApiCache, get, post, setToken } from "../lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [bot, setBot] = useState<string | null>(null);

  useEffect(() => {
    get<{ bot_username: string | null }>("/api/auth/options")
      .then((o) => setBot(o.bot_username))
      .catch(() => undefined);
    // a one-time link from the bot: /login?code=…
    const code = new URLSearchParams(window.location.search).get("code");
    if (!code) return;
    window.history.replaceState(null, "", "/");
    setBusy(true);
    post<{ token: string }>("/api/auth/telegram", { code })
      .then(async ({ token }) => {
        await clearApiCache();
        setToken(token);
        onLogin();
      })
      .catch((err) => setError((err as Error).message))
      .finally(() => setBusy(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await post<{ token: string }>("/api/auth/login", { password });
      await clearApiCache();
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
        {bot && (
          <>
            <p className="small muted" style={{ textAlign: "center" }}>или</p>
            <a className="btn" style={{ width: "100%", textAlign: "center" }} href={`https://t.me/${bot}?start=login`}>
              Войти через Telegram
            </a>
            <p className="small muted">
              Бот пришлёт одноразовую ссылку для входа. Доступ выдаёт владелец — напишите боту /start.
            </p>
          </>
        )}
      </form>
    </div>
  );
}
