import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getToken, post, type Status } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { currentSubscription, isStandalone, pushSupported, subscribePush, unsubscribePush } from "../lib/push";
import { useToast } from "../components/Toast";

function Chip({ on, label }: { on: boolean; label: string }) {
  return <span className={"chip " + (on ? "on" : "off")}>{on ? "●" : "○"} {label}</span>;
}

export default function Settings({ onLogout }: { onLogout: () => void }) {
  const { data: status } = useFetch<Status>("/api/status");
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    currentSubscription().then((s) => setSubscribed(!!s));
  }, []);

  async function togglePush() {
    if (!status) return;
    setBusy(true);
    try {
      if (subscribed) {
        await unsubscribePush();
        setSubscribed(false);
      } else {
        await subscribePush(status.vapid_public_key);
        setSubscribed(true);
        toast("Уведомления включены");
      }
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function testPush() {
    try {
      const { sent } = await post<{ sent: number }>("/api/push/test");
      toast(sent ? `Отправлено на ${sent} устр.` : "Нет подписок");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  const iosHint = /iPhone|iPad/.test(navigator.userAgent) && !isStandalone();

  return (
    <div className="page">
      <h1>Ещё</h1>
      <Link to="/summaries" className="card clickable row between" style={{ display: "flex" }}>
        <span>📝 Саммари и чаты Telegram</span>
        <span className="muted">›</span>
      </Link>
      <Link to="/yougile" className="card clickable row between" style={{ display: "flex" }}>
        <span>🗂 YouGile</span>
        <span className="muted">›</span>
      </Link>

      <div className="card stack">
        <h3>Push-уведомления</h3>
        {!pushSupported() && <p className="muted small">Браузер не поддерживает Web Push.</p>}
        {iosHint && (
          <p className="small" style={{ color: "var(--warn)" }}>
            На iPhone пуши работают только после «Поделиться → На экран “Домой”». Откройте приложение с главного экрана.
          </p>
        )}
        {status && !status.push && <p className="muted small">На сервере не заданы VAPID-ключи.</p>}
        <div className="row wrap">
          <button className="btn primary" onClick={togglePush} disabled={busy || !status?.push || !pushSupported()}>
            {subscribed ? "Отключить на этом устройстве" : "Включить на этом устройстве"}
          </button>
          <button className="btn" onClick={testPush} disabled={!status?.push}>
            Тест
          </button>
        </div>
      </div>

      <div className="card stack">
        <h3>Интеграции</h3>
        {status ? (
          <div className="row wrap">
            <Chip on={status.telegram} label={status.bot_username ? `Telegram @${status.bot_username}` : "Telegram"} />
            <Chip on={status.claude} label={`LLM: ${status.llm_provider} / ${status.llm_model}`} />
            <Chip on={status.google_connected} label="Google Calendar" />
            <Chip on={status.icloud} label="iCloud / Семья" />
            <Chip on={status.yougile} label="YouGile" />
            <Chip on={status.push} label="Web Push" />
          </div>
        ) : (
          <p className="muted">…</p>
        )}
        <p className="small muted">Ключи задаются в .env на сервере, см. README.</p>
      </div>

      <div className="card stack">
        <h3>Напоминания Apple (Команды на iPhone)</h3>
        <p className="small muted">
          Токен подставляется в адрес команды: <code>/api/apple/reminders/sync?token=…</code>. Инструкция в README → «Напоминания Apple».
        </p>
        <button
          className="btn"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(getToken() ?? "");
              toast("Токен скопирован");
            } catch {
              prompt("Скопируйте токен:", getToken() ?? "");
            }
          }}
        >
          Токен для Команд
        </button>
      </div>

      <button className="btn danger" onClick={onLogout}>
        Выйти
      </button>
    </div>
  );
}
